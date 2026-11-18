"""Persistence for the connector: OAuth clients, pending logins, codes, tokens and ERP sessions.

* OAuth access/refresh tokens and authorization codes are stored only as SHA-256 hashes.
* ERP tokens are encrypted with Fernet (ERP_MCP_ENCRYPTION_KEY).
* Passwords are never stored.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY, info_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pending_logins (
    id TEXT PRIMARY KEY, csrf TEXT NOT NULL, client_id TEXT NOT NULL, params_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, session_id TEXT, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS auth_codes (
    code_hash TEXT PRIMARY KEY, data_json TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL, scopes TEXT NOT NULL,
    session_id TEXT NOT NULL, resource TEXT, family TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_tokens_family ON oauth_tokens(family);
CREATE TABLE IF NOT EXISTS erp_sessions (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, erp_token_enc TEXT NOT NULL, instances_json TEXT NOT NULL,
    selected_instance_id TEXT, erp_expires_at REAL NOT NULL, created_at REAL NOT NULL);
"""


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


@dataclass
class ErpSession:
    id: str
    user_id: str
    erp_token: str
    instances: list[dict]  # [{"id": "<encrypted InstanceId>", "name": "Party name"}]
    selected_instance_id: str | None
    erp_expires_at: float

    @property
    def selected(self) -> dict | None:
        return next((i for i in self.instances if i["id"] == self.selected_instance_id), None)


class Store:
    def __init__(self, path: str, encryption_key: str):
        if not encryption_key:
            raise RuntimeError("ERP_MCP_ENCRYPTION_KEY is required (a Fernet key).")
        self._fernet = Fernet(encryption_key.encode())
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    # ------------------------------------------------------------------ low level
    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def _one(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def purge_expired(self) -> None:
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM pending_logins WHERE expires_at < ?", (now,))
            self._db.execute("DELETE FROM auth_codes WHERE expires_at < ?", (now,))
            self._db.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (now,))
            self._db.execute(
                "DELETE FROM erp_sessions WHERE erp_expires_at < ? AND id NOT IN (SELECT session_id FROM oauth_tokens)",
                (now,))

    # ------------------------------------------------------------------ clients
    def save_client(self, client_id: str, info_json: str) -> None:
        self._exec("INSERT OR REPLACE INTO oauth_clients VALUES (?,?,?)", (client_id, info_json, time.time()))

    def all_clients(self) -> list[tuple[str, str]]:
        with self._lock:
            return self._db.execute("SELECT client_id, info_json FROM oauth_clients").fetchall()

    def delete_client(self, client_id: str) -> None:
        self._exec("DELETE FROM oauth_clients WHERE client_id=?", (client_id,))

    def get_client(self, client_id: str) -> str | None:
        row = self._one("SELECT info_json FROM oauth_clients WHERE client_id=?", (client_id,))
        return row[0] if row else None

    # ------------------------------------------------------------------ pending logins
    def create_pending(self, client_id: str, params_json: str, ttl: int) -> tuple[str, str]:
        pid, csrf = new_secret(), new_secret(16)
        self._exec("INSERT INTO pending_logins (id, csrf, client_id, params_json, expires_at) VALUES (?,?,?,?,?)",
                   (pid, csrf, client_id, params_json, time.time() + ttl))
        return pid, csrf

    def get_pending(self, pid: str) -> dict | None:
        row = self._one("SELECT id, csrf, client_id, params_json, attempts, session_id FROM pending_logins "
                        "WHERE id=? AND expires_at>=?", (pid, time.time()))
        if not row:
            return None
        return dict(zip(["id", "csrf", "client_id", "params_json", "attempts", "session_id"], row))

    def bump_attempts(self, pid: str) -> None:
        self._exec("UPDATE pending_logins SET attempts=attempts+1 WHERE id=?", (pid,))

    def attach_session(self, pid: str, session_id: str) -> None:
        self._exec("UPDATE pending_logins SET session_id=? WHERE id=?", (session_id, pid))

    def delete_pending(self, pid: str) -> None:
        self._exec("DELETE FROM pending_logins WHERE id=?", (pid,))

    # ------------------------------------------------------------------ auth codes
    def save_code(self, code: str, data: dict, expires_at: float) -> None:
        self._exec("INSERT INTO auth_codes VALUES (?,?,?)", (token_hash(code), json.dumps(data), expires_at))

    def get_code(self, code: str) -> dict | None:
        row = self._one("SELECT data_json, expires_at FROM auth_codes WHERE code_hash=? AND expires_at>=?",
                        (token_hash(code), time.time()))
        return {**json.loads(row[0]), "expires_at": row[1]} if row else None

    def delete_code(self, code: str) -> None:
        self._exec("DELETE FROM auth_codes WHERE code_hash=?", (token_hash(code),))

    # ------------------------------------------------------------------ oauth tokens
    def save_token(self, value: str, kind: str, client_id: str, scopes: list[str], session_id: str,
                   resource: str | None, family: str, expires_at: float) -> None:
        self._exec("INSERT INTO oauth_tokens VALUES (?,?,?,?,?,?,?,?)",
                   (token_hash(value), kind, client_id, " ".join(scopes), session_id, resource, family, expires_at))

    def get_token(self, value: str, kind: str) -> dict | None:
        row = self._one("SELECT client_id, scopes, session_id, resource, family, expires_at FROM oauth_tokens "
                        "WHERE token_hash=? AND kind=? AND expires_at>=?", (token_hash(value), kind, time.time()))
        if not row:
            return None
        d = dict(zip(["client_id", "scopes", "session_id", "resource", "family", "expires_at"], row))
        d["scopes"] = d["scopes"].split() if d["scopes"] else []
        return d

    def revoke_family(self, family: str) -> None:
        self._exec("DELETE FROM oauth_tokens WHERE family=?", (family,))

    # ------------------------------------------------------------------ erp sessions
    def create_session(self, user_id: str, erp_token: str, instances: list[dict], erp_expires_at: float) -> str:
        sid = new_secret()
        selected = instances[0]["id"] if len(instances) == 1 else None
        self._exec("INSERT INTO erp_sessions VALUES (?,?,?,?,?,?,?)",
                   (sid, user_id, self._fernet.encrypt(erp_token.encode()).decode(), json.dumps(instances),
                    selected, erp_expires_at, time.time()))
        return sid

    def get_session(self, sid: str) -> ErpSession | None:
        row = self._one("SELECT id, user_id, erp_token_enc, instances_json, selected_instance_id, erp_expires_at "
                        "FROM erp_sessions WHERE id=?", (sid,))
        if not row:
            return None
        try:
            erp_token = self._fernet.decrypt(row[2].encode()).decode()
        except InvalidToken:
            return None
        return ErpSession(id=row[0], user_id=row[1], erp_token=erp_token, instances=json.loads(row[3]),
                          selected_instance_id=row[4], erp_expires_at=row[5])

    def select_instance(self, sid: str, instance_id: str) -> None:
        self._exec("UPDATE erp_sessions SET selected_instance_id=? WHERE id=?", (instance_id, sid))
