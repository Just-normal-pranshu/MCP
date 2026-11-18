"""Fetches raw data from the three stored procedures.

Only these three procedures can ever be executed: the names are constants below, never taken from a
request, and every argument is passed as a bound parameter.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import OrderedDict
from datetime import date
from pathlib import Path

import pandas as pd

from .config import get_settings
from .security import Tenant

log = logging.getLogger("data_api.sources")

SP_SALES = "ProcAPIUNISalesData"
SP_PROGRESS = "ProcAPIUNISalesProgress"
SP_STOCK = "ProcAPIUNIStockData"

# procedure -> ordered list of parameter names it accepts
SP_PARAMS: dict[str, list[str]] = {
    SP_SALES: ["@dFromDt", "@dToDt", "@cBranchId"],
    SP_PROGRESS: ["@dFromDt", "@dToDt", "@cBranchId"],
    SP_STOCK: ["@cBranchId"],
}


class DataSourceError(Exception):
    """Raised when the database call fails. The message is safe to show to API callers."""


# --------------------------------------------------------------------------- connection strings
_KEY_MAP = {
    "data source": "SERVER", "datasource": "SERVER", "server": "SERVER", "address": "SERVER", "addr": "SERVER",
    "network address": "SERVER", "server name": "SERVER",
    "initial catalog": "DATABASE", "initialcatalog": "DATABASE", "database": "DATABASE",
    "user id": "UID", "userid": "UID", "uid": "UID", "user": "UID", "username": "UID", "user name": "UID",
    "password": "PWD", "pwd": "PWD",
}


def _split_conn(conn: str) -> list[tuple[str, str]]:
    """Split 'k=v;k=v' respecting values wrapped in double quotes or braces."""
    pairs, buf, quote = [], "", None
    for ch in conn + ";":
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in "\"{":
            quote = "}" if ch == "{" else ch
            buf += ch
        elif ch == ";":
            if "=" in buf:
                k, v = buf.split("=", 1)
                pairs.append((k.strip(), v.strip()))
            buf = ""
        else:
            buf += ch
    return pairs


def _unwrap(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == "{" and v[-1] == "}":
        return v[1:-1].replace("}}", "}")          # ODBC-style braces: }} is an escaped }
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        q = v[0]
        return v[1:-1].replace(q + q, q)           # ADO.NET quotes: doubled quote is an escaped quote
    return v.replace("&quot;", '"')


def dotnet_to_odbc(conn: str, driver: str) -> str:
    """Convert an ADO.NET connection string (what the ERP stores) into an ODBC one for pyodbc.

    Also accepts Entity Framework strings (uses the inner 'provider connection string')
    and strings wrapped in quotes.
    """
    conn = _unwrap(conn or "")
    pairs = _split_conn(conn)
    inner = next((v for k, v in pairs if k.lower() == "provider connection string"), None)
    if inner is not None:
        pairs = _split_conn(_unwrap(inner))

    parts: dict[str, str] = {}
    for k, v in pairs:
        mapped = _KEY_MAP.get(" ".join(k.lower().split()))
        if mapped:
            parts[mapped] = _unwrap(v)
    if "SERVER" not in parts or "DATABASE" not in parts:
        found = ", ".join(k for k, _ in pairs) or "none (empty string?)"
        log.error("Connection string has no server/database. Keys found: %s", found)
        raise DataSourceError("Company database is not configured correctly. Please contact the provider.")

    def q(v: str) -> str:  # ODBC values containing ; or } must be wrapped in braces
        return "{" + v.replace("}", "}}") + "}" if any(c in v for c in ";{}") else v

    out = [f"DRIVER={{{driver}}}", f"SERVER={q(parts['SERVER'])}", f"DATABASE={q(parts['DATABASE'])}"]
    if "UID" in parts:
        out += [f"UID={q(parts['UID'])}", f"PWD={q(parts.get('PWD', ''))}"]
    else:
        out.append("Trusted_Connection=yes")
    st = get_settings()
    out += [f"Encrypt={'yes' if st.sql_encrypt else 'no'}",
            f"TrustServerCertificate={'yes' if st.sql_trust_server_certificate else 'no'}"]
    return ";".join(out) + ";"


# --------------------------------------------------------------------------- SQL Server
def _run_sql(tenant: Tenant, sp: str, args: list[str]) -> pd.DataFrame:
    import pyodbc  # imported lazily so fixture mode works without the ODBC driver

    s = get_settings()
    names = SP_PARAMS[sp]
    sql = f"SET NOCOUNT ON; EXEC {sp} " + ", ".join(f"{n} = ?" for n in names)
    try:
        with pyodbc.connect(dotnet_to_odbc(tenant.connection_string, s.odbc_driver), timeout=15) as cn:
            cn.timeout = s.sql_timeout_seconds
            cur = cn.cursor()
            cur.execute(sql, args)
            while cur.description is None:  # skip row-count / message result sets
                if not cur.nextset():
                    return pd.DataFrame()
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return pd.DataFrame.from_records([tuple(r) for r in rows], columns=cols)
    except pyodbc.Error as e:
        sqlstate = str(e.args[0]) if e.args else "?"
        detail = str(e.args[1]) if len(e.args) > 1 else str(e)
        detail = re.sub(r"(?i)(pwd|password)=[^;]*", r"\1=***", detail)[:300]  # never log a password
        log.error("SP %s failed for instance %s: %s [SQLSTATE %s] %s", sp, tenant.name or tenant.instance_id[:8],
                  e.__class__.__name__, sqlstate, detail)
        low = detail.lower()
        if "could not find stored procedure" in low:
            raise DataSourceError("Reports are not set up for this company yet (required ERP procedures are missing). "
                                  "Please contact the provider.") from e
        if "permission" in low and "denied" in low:
            raise DataSourceError("The reporting login has no permission to run the ERP report procedures for this "
                                  "company. Please contact the provider.") from e
        raise DataSourceError("Could not read data from the company database. Try again shortly.") from e


# --------------------------------------------------------------------------- fixture (dev only)
_FIXTURE_FILES = {SP_SALES: "sales_data.xlsx", SP_PROGRESS: "sales_progress.xlsx", SP_STOCK: "stock_data.xlsx"}


def _run_fixture(sp: str, args: list[str]) -> pd.DataFrame:
    path = Path(get_settings().fixture_dir) / _FIXTURE_FILES[sp]
    df = pd.read_excel(path)
    params = dict(zip(SP_PARAMS[sp], args))
    if sp == SP_SALES:  # emulate the SP's own filtering
        dt = pd.to_datetime(df["InvoiceDt"])
        df = df[(dt >= pd.Timestamp(params["@dFromDt"])) & (dt <= pd.Timestamp(params["@dToDt"]))]
    branch = params.get("@cBranchId")
    if branch and "BranchId" in df.columns:
        df = df[df["BranchId"].astype(str) == str(branch)]
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- cache
class _TTLCache:
    def __init__(self):
        self._d: OrderedDict[tuple, tuple[float, pd.DataFrame]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> pd.DataFrame | None:
        s = get_settings()
        with self._lock:
            hit = self._d.get(key)
            if not hit:
                return None
            if time.monotonic() - hit[0] > s.cache_ttl_seconds:
                self._d.pop(key, None)
                return None
            self._d.move_to_end(key)
            return hit[1]

    def put(self, key: tuple, df: pd.DataFrame) -> None:
        s = get_settings()
        with self._lock:
            self._d[key] = (time.monotonic(), df)
            self._d.move_to_end(key)
            while len(self._d) > s.cache_max_entries:
                self._d.popitem(last=False)


_cache = _TTLCache()
_inflight: dict[tuple, asyncio.Future] = {}


def _fmt(d: date | None) -> str:
    return d.isoformat() if d else ""


async def fetch(tenant: Tenant, sp: str, *, from_dt: date | None = None, to_dt: date | None = None,
                branch_id: str | None = None, normalize=None) -> pd.DataFrame:
    """Run (or reuse a cached run of) a stored procedure and return a DataFrame.

    `normalize` is applied once before caching so every summary works on clean data.
    Concurrent identical requests share one database call.
    """
    if sp not in SP_PARAMS:
        raise ValueError("unknown procedure")
    if sp == SP_STOCK:
        args = [branch_id or ""]
    else:
        args = [_fmt(from_dt), _fmt(to_dt), branch_id or ""]
    key = (tenant.cache_key, sp, tuple(args))

    cached = _cache.get(key)
    if cached is not None:
        return cached
    if key in _inflight:
        return await asyncio.shield(_inflight[key])

    fut = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    try:
        if get_settings().data_source == "fixture":
            df = await asyncio.to_thread(_run_fixture, sp, args)
        else:
            df = await asyncio.to_thread(_run_sql, tenant, sp, args)
        if normalize is not None:
            df = normalize(df)
        _cache.put(key, df)
        fut.set_result(df)
        return df
    except BaseException as e:
        fut.set_exception(e)
        fut.exception()  # mark retrieved so asyncio does not warn when nobody else awaited
        raise
    finally:
        _inflight.pop(key, None)
