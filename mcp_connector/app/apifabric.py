"""Login against APIFabric: POST {apifabric_url}/access_token {UserId, Password, Mode}.

Only the token, the expiry and each instance's Id/Name are kept from the response. Anything else the
response may carry (connection strings, profiles) is discarded immediately.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

log = logging.getLogger("erp_mcp.apifabric")


class LoginError(Exception):
    """Login failed; the message is safe to show on the login page."""


@dataclass
class LoginResult:
    user_id: str
    token: str
    instances: list[dict]  # [{"id": ..., "name": ...}]
    expires_at: float


def _get(d: dict, *names: str):
    for n in names:
        for k, v in d.items():
            if k.lower() == n.lower():
                return v
    return None


def _parse_validity(value, fallback_days: int) -> float:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time() + fallback_days * 86400


def _branch_hint(inst: dict) -> str | None:
    """Something that tells two same-named companies apart: the profile's branch name, else the BranchId."""
    profile = _get(inst, "UserProfile")
    if isinstance(profile, dict):
        for key in ("BrName", "BranchName", "HoBranchName", "BranchAlias"):
            v = _get(profile, key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    v = _get(inst, "BranchId")
    return str(v).strip() if v not in (None, "") else None


def disambiguate(instances: list[dict]) -> list[dict]:
    """Make company labels unique.

    Same-named companies get ' - <branch>' only when that actually tells them apart;
    anything still equal gets ' (1)', ' (2)'.
    """
    groups: dict[str, list[dict]] = {}
    for i in instances:
        groups.setdefault(i["name"].lower(), []).append(i)
    for group in groups.values():
        hints = [g.get("hint") for g in group]
        if len(group) > 1 and all(hints) and len({h.lower() for h in hints}) > 1:
            for g in group:
                g["name"] = f'{g["name"]} - {g["hint"]}'
    groups = {}
    for i in instances:
        groups.setdefault(i["name"].lower(), []).append(i)
    for group in groups.values():
        if len(group) > 1:
            for n, g in enumerate(group, 1):
                g["name"] = f'{g["name"]} ({n})'
    return [{"id": i["id"], "name": i["name"]} for i in instances]


async def login(base_url: str, user_id: str, password: str, mode: int, fallback_days: int) -> LoginResult:
    url = f"{base_url.rstrip('/')}/access_token"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json={"UserId": user_id, "Password": password, "Mode": mode})
    except httpx.HTTPError as e:
        log.error("APIFabric unreachable: %s", e.__class__.__name__)
        raise LoginError("zOrder login service is not reachable right now. Try again in a minute.")

    try:
        body = resp.json()
    except ValueError:
        log.error("APIFabric returned non-JSON (HTTP %s)", resp.status_code)
        raise LoginError("zOrder login service returned an unexpected response.")

    if not _get(body, "Success"):
        msg = (_get(body, "Message") or "").strip()
        raise LoginError(msg or "Email or password is incorrect.")

    data = _get(body, "Data") or {}
    token = _get(data, "Token")
    if not token:
        raise LoginError("Login succeeded but no session token was returned. Contact zOrder support.")

    raw = _get(data, "Instances")
    if not isinstance(raw, list):
        single = _get(data, "Instance")
        raw = [single] if isinstance(single, dict) else []
    instances = []
    for inst in raw:
        iid = _get(inst, "Id", "InstanceId")
        name = _get(inst, "Name", "PartyName") or "Company"
        if iid:
            instances.append({"id": str(iid), "name": str(name).strip(), "hint": _branch_hint(inst)})
    if not instances:
        raise LoginError("This account has no company with admin access.")
    instances = disambiguate(instances)

    return LoginResult(user_id=user_id, token=str(token), instances=instances,
                       expires_at=_parse_validity(_get(data, "TokenValidity"), fallback_days))
