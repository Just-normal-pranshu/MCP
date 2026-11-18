"""End-to-end check of the connector: OAuth (registration, PKCE, login page, company choice), MCP tools,
token refresh. Prints PASS/FAIL per check and exits non-zero if anything fails.

Needs the Data API (fixture mode), the mock login service and the connector running; dev/run_demo.py
starts all three and runs this file for you.

    python dev/smoke_test.py [connector base URL] [resource URL] [redirect URI]
"""
import base64
import hashlib
import json
import re
import secrets
import sys
from urllib.parse import parse_qs, urlparse

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8810"
RESOURCE = sys.argv[2] if len(sys.argv) > 2 else BASE.rstrip("/") + "/mcp"
REDIRECT = sys.argv[3] if len(sys.argv) > 3 else "https://claude.ai/api/mcp/auth_callback"
FIXTURE_RANGE = {"from_date": "2026-04-01", "to_date": "2026-10-31"}  # dates covered by dev/fixtures

c = httpx.Client(base_url=BASE, follow_redirects=False, timeout=120)
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def path_of(url: str) -> str:
    u = urlparse(url)
    prefix = urlparse(BASE).path.rstrip("/")
    return (u.path[len(prefix):] if prefix and u.path.startswith(prefix) else u.path) + "?" + u.query


# ---- OAuth -----------------------------------------------------------------------------------
r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
           headers={"Accept": "application/json, text/event-stream"})
check("MCP endpoint asks for sign-in (401 + resource metadata)",
      r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", ""))

bad = c.post("/register", json={"redirect_uris": ["https://evil.example/cb"], "client_name": "evil",
                                "token_endpoint_auth_method": "client_secret_post"})
check("Registration with a foreign redirect URI is refused", bad.status_code == 400)

reg = c.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "smoke-test",
                                "grant_types": ["authorization_code", "refresh_token"],
                                "token_endpoint_auth_method": "client_secret_post"}).json()
cid, csec = reg["client_id"], reg.get("client_secret")
check("Client registration with Claude's callback", bool(cid))

verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
r = c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
                                "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
                                "scope": "erp.read", "resource": RESOURCE})
check("Authorize redirects to the login page", r.status_code == 302 and "/login?req=" in r.headers.get("location", ""))
page = c.get(path_of(r.headers["location"])).text
req = re.search(r'name="req" value="([^"]+)"', page).group(1)
csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
check("Login page shows who is asking", "Returns to" in page)

r = c.post("/login", data={"req": req, "csrf": csrf, "email": "admin@demo.test", "password": "wrong"})
check("Wrong password is rejected on the page", r.status_code == 401)
r = c.post("/login", data={"req": req, "csrf": csrf, "email": "admin@demo.test", "password": "demo123"})
check("Right password continues", r.status_code == 303)
page = c.get(f"/login?req={req}").text
check("Company picker lists both demo companies", "zOrder Testing" in page and "Second Demo Co" in page)
r = c.post("/login/company", data={"req": req, "csrf": csrf, "instance": "DEMO-INSTANCE-1"})
cb = r.headers.get("location", "")
q = parse_qs(urlparse(cb).query)
check("Company choice returns a code to Claude's callback", cb.startswith(REDIRECT) and q.get("state") == ["xyz"])

tok = c.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
                             "client_id": cid, "client_secret": csec, "code_verifier": verifier,
                             "resource": RESOURCE}).json()
check("Code exchanged for access + refresh tokens", "access_token" in tok and "refresh_token" in tok, str(tok)[:120])
r = c.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
                           "client_id": cid, "client_secret": csec, "code_verifier": verifier})
check("A used code cannot be reused", r.status_code == 400)

# ---- MCP tools -------------------------------------------------------------------------------
H = {"Authorization": f"Bearer {tok['access_token']}", "Accept": "application/json, text/event-stream"}
_id = [10]


def rpc(method, params=None):
    _id[0] += 1
    resp = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params or {}})
    return resp.status_code, resp.json()


st, _ = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "smoke-test", "version": "1"}})
check("MCP initialize", st == 200)
_, tl = rpc("tools/list")
tools = {t["name"]: t for t in tl["result"]["tools"]}
check("10 tools listed", len(tools) == 10, ", ".join(tools))
check("Every tool is marked read-only", all((t.get("annotations") or {}).get("readOnlyHint") for t in tools.values()))


def tool(name, args):
    _, res = rpc("tools/call", {"name": name, "arguments": args})
    r = res.get("result", res)
    txt = r["content"][0]["text"] if r.get("content") else json.dumps(r)
    return bool(r.get("isError")), txt


cases = [  # (label, tool, args, expect_error, must_contain)
    ("Status shows the active company", "erp_status", {}, False, "zOrder Testing"),
    ("Sales by brand", "erp_sales_summary", {"group_by": ["brand"], "top_n": 3, **FIXTURE_RANGE}, False, "net_sale"),
    ("Monthly sales for a branch by name", "erp_sales_summary",
     {"group_by": ["month"], "branch": "Store 001", **FIXTURE_RANGE}, False, "2026-"),
    ("Stock by age", "erp_stock_summary", {"group_by": ["aging"]}, False, "365+ days"),
    ("FY vs last FY", "erp_sales_progress", {}, False, "like_for_like"),
    ("Customer lookup", "erp_customer_lookup", {"name": "Customer 00", "limit": 2, **FIXTURE_RANGE}, False, "Customer"),
    ("Supplier summary", "erp_supplier_summary", {"top_n": 2, **FIXTURE_RANGE}, False, "Supplier"),
    ("Unknown branch gives a helpful error", "erp_sales_summary", {"branch": "nowhere"}, True, "Branches:"),
    ("Switch company", "erp_switch_company", {"company_name": "second"}, False, "Second Demo Co"),
    ("Company without data access is refused", "erp_branches", {}, True, "no data access"),
]
for label, name, args, expect_error, must in cases:
    err, txt = tool(name, args)
    check(label, err == expect_error and must in txt, txt[:160])

# ---- refresh -------------------------------------------------------------------------------
new = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                             "client_id": cid, "client_secret": csec}).json()
check("Refresh issues a new access token", "access_token" in new)
old = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 99, "method": "tools/list"})
check("Old access token stops working after refresh", old.status_code == 401)

print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
