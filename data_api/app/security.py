"""Turns the ERP headers (Z-Auth-Token + Instance_Id) into a tenant: which client database to query.

Every request must pass this check BEFORE any database is touched. The resolver is the only place
that knows connection strings, so company isolation lives here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException, Request

from .config import get_settings

log = logging.getLogger("data_api.security")


@dataclass(frozen=True)
class Tenant:
    instance_id: str
    connection_string: str  # .NET style ("Data Source=...;Initial Catalog=...") or "fixture"
    name: str | None = None

    @property
    def cache_key(self) -> str:
        return self.instance_id


class ConfigError(Exception):
    """The Data API itself is misconfigured (e.g. instances file missing or invalid)."""


class CompanyAccessDenied(Exception):
    """The token is valid but cannot read this company (or the company is not configured)."""


class Resolver(Protocol):
    async def resolve(self, token: str, instance_id: str) -> Tenant | None:
        """Return the tenant, None if the token is invalid/expired, or raise CompanyAccessDenied."""
        ...


class StaticResolver:
    """Development resolver backed by instances.json.

    {
      "instances": {
        "<Instance_Id exactly as sent in the header>": {
          "name": "zOrder Testing",
          "connection_string": "Data Source=...;Initial Catalog=...;User ID=...;Password=...;",
          "tokens": ["<allowed Z-Auth-Token>", "..."]
        }
      }
    }

    If "tokens" is present, only those tokens may read that instance. Without it any non-empty token is
    accepted, which is only acceptable on a developer machine.
    """

    def __init__(self, path: str):
        self._path = Path(path)
        self._data: dict = {}
        self._mtime = 0.0

    def _load(self) -> dict:
        if not self._path.exists():
            raise ConfigError(f"instances file not found: {self._path.resolve()}")
        mtime = self._path.stat().st_mtime
        if mtime != self._mtime:
            try:
                # utf-8-sig also accepts files saved with a BOM (Windows PowerShell / Notepad)
                self._data = json.loads(self._path.read_text(encoding="utf-8-sig")).get("instances", {})
            except (ValueError, AttributeError) as e:
                raise ConfigError(f"instances file is not valid JSON: {self._path.resolve()} ({e})")
            self._mtime = mtime
        return self._data

    async def resolve(self, token: str, instance_id: str) -> Tenant | None:
        data = self._load()
        known_tokens = {t for e in data.values() for t in (e.get("tokens") or [])}
        if known_tokens and token not in known_tokens:
            return None
        entry = data.get(instance_id)
        if not entry:
            raise CompanyAccessDenied()
        allowed = entry.get("tokens")
        if not allowed or token not in allowed:  # an entry without a tokens list grants nothing
            raise CompanyAccessDenied()
        return Tenant(instance_id=instance_id, connection_string=entry["connection_string"], name=entry.get("name"))


class TokenStoreResolver:
    """Production resolver: validate the token in the zSupport token store and pick the instance's ConnString.

    Pending inputs (see README "Pending for production"):
      * where TokenZ._Write stores sessions (table / columns) and how TokenValue is shaped
      * whether Session_Instance.ConnString / InstanceId are serialised into TokenValue
      * CryptoEncrypt / CryptoDecrypt (Instance_Id in headers is encrypted; ConnString password is encrypted)
    Expected logic once known:
      1. SELECT TokenValue, TokenValidity FROM <token table> WHERE Token = ?   (parameterised)
      2. return None if missing or TokenValidity < now
      3. find the instance whose (encrypted) InstanceId == Instance_Id header, else raise CompanyAccessDenied
      4. return Tenant(instance_id, decrypted ConnString, PartyName)
    """

    async def resolve(self, token: str, instance_id: str) -> Tenant | None:  # pragma: no cover
        raise NotImplementedError("TokenStoreResolver is not wired yet; use DATA_API_RESOLVER=static for now.")


def _ci(d: dict, *names: str):
    """Case-insensitive lookup of the first matching key."""
    lower = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, ""):
            return v
    return None


def _quote(v: str) -> str:
    """ADO.NET-style quoting for values with ; or quotes (understood by sources.dotnet_to_odbc)."""
    return "{" + v.replace("}", "}}") + "}" if any(c in v for c in ';{}"\'') else v


UNAVAILABLE_MSG = "Could not connect to server. Please contact the provider."


class ConnectionServiceError(Exception):
    """The ERP connection API is unreachable or answered unexpectedly. Shown to users as UNAVAILABLE_MSG."""


class AppFabricResolver:
    """Asks the ERP's connection API for the selected company's database details.

    Request:  {method} {conn_api_url}
              headers  Z-Auth-Token, Instance_Id (and the service key header if configured)
              POST additionally sends JSON {"Token": ..., "InstanceId": ...}
    Response: {"Success": true, "Data": {"ServerName", "DatabaseName", "UserName", "Password", ["Port"],
               ["Name"], ["TokenValidity"]}}  (field names matched case-insensitively, common aliases
               accepted; a ready "ConnString" is also accepted)
    401/403 or Success=false -> the token is not valid for that company.
    Resolved connections are cached in memory per token+company; nothing is written to disk or logs.
    """

    SERVER = ("ServerName", "Server", "ServerIP", "SqlServer", "SqlServerIP", "DataSource", "Data Source", "Host")
    DATABASE = ("DatabaseName", "Database", "InitialCatalog", "Initial Catalog", "DbName", "SqlDbName",
                "SqlDatabase", "SqlDb")
    USER = ("UserName", "UserId", "User ID", "User", "Uid", "Login", "SqlUsrId", "SqlUserId", "SqlUser")
    PASSWORD = ("Password", "Pwd", "SqlPwd", "SqlPassword")
    PORT = ("Port",)
    CONN = ("ConnString", "ConnectionString")
    NAME = ("Name", "PartyName", "CompanyName", "InstanceName")

    def __init__(self):
        import httpx
        s = get_settings()
        if not s.conn_api_url:
            raise ConfigError("DATA_API_CONN_API_URL is not set.")
        self.s = s
        if not s.conn_api_key:
            log.warning("DATA_API_CONN_API_KEY is empty: the connection API is called without a service key. "
                        "Make sure it is IP-restricted to this server.")
        self._client = httpx.AsyncClient(timeout=s.conn_api_timeout_seconds)
        self._cache: dict[str, tuple[float, Tenant | None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _key(self, token: str, instance_id: str) -> str:
        return hashlib.sha256(f"{token}\x00{instance_id}".encode()).hexdigest()

    async def resolve(self, token: str, instance_id: str) -> Tenant | None:
        key = self._key(token, instance_id)
        hit = self._cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit and hit[0] > time.time():
                return hit[1]
            tenant, ttl = await self._fetch(token, instance_id)
            self._cache[key] = (time.time() + ttl, tenant)
            if len(self._cache) > 5000:  # crude bound
                now = time.time()
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            return tenant

    async def _fetch(self, token: str, instance_id: str) -> tuple[Tenant | None, float]:
        import httpx
        headers = {"Z-Auth-Token": token, "Instance_Id": instance_id, "Instance-Id": instance_id,
                   "Accept": "application/json"}
        if self.s.conn_api_key:
            headers[self.s.conn_api_key_header] = self.s.conn_api_key
        try:
            if self.s.conn_api_method == "POST":
                resp = await self._client.post(self.s.conn_api_url, headers=headers,
                                               json={"Token": token, "InstanceId": instance_id})
            else:
                resp = await self._client.get(self.s.conn_api_url, headers=headers)
        except httpx.HTTPError as e:
            log.error("Connection API unreachable: %s", e.__class__.__name__)
            raise ConnectionServiceError(UNAVAILABLE_MSG)

        if resp.status_code == 401:
            return None, 30  # token not valid: short negative cache
        if resp.status_code == 403:
            # Refused: usually this server's IP is not on the connection API's allowlist.
            log.error("Connection API refused the request (HTTP 403). Check that this server's IP is on its "
                      "allowlist. Response: %s", resp.text[:200].replace("\n", " "))
            raise ConnectionServiceError(UNAVAILABLE_MSG)
        try:
            body = resp.json()
        except ValueError:
            log.error("Connection API returned non-JSON (HTTP %s)", resp.status_code)
            raise ConnectionServiceError(UNAVAILABLE_MSG)
        if resp.status_code >= 400 or not isinstance(body, dict):
            log.error("Connection API error HTTP %s", resp.status_code)
            raise ConnectionServiceError(UNAVAILABLE_MSG)
        if _ci(body, "Success") is False:
            return None, 30

        data = _ci(body, "Data")
        data = data if isinstance(data, dict) else body
        conn = _ci(data, *self.CONN)
        if not conn:
            def pick(override: str, aliases: tuple):
                return _ci(data, override) if override else _ci(data, *aliases)
            server = pick(self.s.conn_field_server, self.SERVER)
            db = pick(self.s.conn_field_database, self.DATABASE)
            if not server or not db:
                log.error("Connection API response lacks %s (keys received: %s). Set DATA_API_CONN_FIELD_SERVER / "
                          "DATA_API_CONN_FIELD_DATABASE to the right field names.",
                          "server" if not server else "database", ", ".join(map(str, data.keys())))
                raise ConnectionServiceError(UNAVAILABLE_MSG)
            port = _ci(data, *self.PORT)
            server = f"{server},{port}" if port and "," not in str(server) else str(server)
            parts = [f"Data Source={_quote(server)}", f"Initial Catalog={_quote(str(db))}"]
            user = pick(self.s.conn_field_user, self.USER)
            if user:
                pwd = pick(self.s.conn_field_password, self.PASSWORD) or ""
                parts += [f"User ID={_quote(str(user))}", f"Password={_quote(str(pwd))}"]
            conn = ";".join(parts) + ";"

        ttl = float(self.s.conn_cache_seconds)
        validity = _ci(data, "TokenValidity") or _ci(body, "TokenValidity")
        if isinstance(validity, str):
            try:
                from datetime import datetime
                left = datetime.fromisoformat(validity.replace("Z", "+00:00")).timestamp() - time.time()
                if left <= 0:
                    return None, 30
                ttl = min(ttl, left)
            except ValueError:
                pass
        return Tenant(instance_id=instance_id, connection_string=str(conn),
                      name=_ci(data, *self.NAME)), ttl


_resolver: Resolver | None = None


def get_resolver() -> Resolver:
    global _resolver
    if _resolver is None:
        s = get_settings()
        if s.resolver != "appfabric" and not s.allow_dev_modes:
            raise ConfigError(f"DATA_API_RESOLVER={s.resolver} is a development mode; set DATA_API_ALLOW_DEV_MODES=true "
                              "to use it (never in production).")
        if s.resolver == "appfabric":
            _resolver = AppFabricResolver()
        elif s.resolver == "static":
            _resolver = StaticResolver(s.instances_file)
        else:
            _resolver = TokenStoreResolver()
    return _resolver


async def require_tenant(request: Request) -> Tenant:
    """FastAPI dependency. Header names follow the ERP: Z-Auth-Token and Instance_Id.

    Note: nginx drops headers containing underscores unless `underscores_in_headers on;` is set,
    so Instance-Id (hyphen) is accepted as well.
    """
    token = (request.headers.get("z-auth-token") or "").strip()
    instance_id = (request.headers.get("instance_id") or request.headers.get("instance-id") or "").strip()
    if not token or not instance_id:
        raise HTTPException(status_code=401, detail="Z-Auth-Token and Instance_Id headers are required.")
    try:
        tenant = await get_resolver().resolve(token, instance_id)  # may raise ConfigError on first use
    except NotImplementedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ConnectionServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ConfigError as e:
        log.error("Data API configuration error: %s", e)
        raise HTTPException(status_code=500, detail="Data API is misconfigured. Please contact the provider.")
    except CompanyAccessDenied:
        raise HTTPException(status_code=403, detail="This login has no data access for the selected company.")
    if tenant is None:
        raise HTTPException(status_code=401, detail="Token is invalid or expired.")
    return tenant
