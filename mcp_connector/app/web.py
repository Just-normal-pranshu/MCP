"""Login page and company picker shown during the OAuth flow."""
from __future__ import annotations

import hmac
import logging
from html import escape

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import apifabric
from .config import Settings
from .oauth import ErpOAuthProvider
from .store import Store

log = logging.getLogger("erp_mcp.web")

def _headers(settings: Settings) -> dict:
    from urllib.parse import urlparse
    origins = sorted({f"{urlparse(u).scheme}://{urlparse(u).netloc}" for u in settings.redirect_allowlist})
    if settings.allow_localhost_redirects:
        origins += ["http://localhost:*", "http://127.0.0.1:*"]
    return {
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
                                   "font-src https://fonts.gstatic.com; form-action 'self' " + " ".join(origins) +
                                   "; base-uri 'none'",
    }

_CSS = """
:root{--brand:#3E9BE9;--accent:#2563eb;--ink:#14263a;--muted:#5a6b7d;--line:#d9e2ec;--bg:#f3f6f9;--card:#fff;
--err-bg:#fdecec;--err:#a32d2d;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--ink:#e7eef6;--muted:#9fb0c2;--line:#2b3a4b;--bg:#0f1823;--card:#16222f;
--err-bg:#3a1a1a;--err:#f4b4b4;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);font:16px/1.55 "DM Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
display:flex;align-items:center;justify-content:center;padding:24px}
main{width:100%;max-width:400px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:32px 28px}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:28px;font-weight:600;letter-spacing:-.01em}
.mark{width:30px;height:30px;border-radius:8px;background:var(--brand);display:grid;place-items:center}
.mark span{width:12px;height:12px;border-radius:50%;border:3px solid #fff}
.brand small{font-weight:400;color:var(--muted)}
h1{font-size:22px;line-height:1.25;margin:0 0 8px;letter-spacing:-.02em;font-weight:600}
p.lead{margin:0 0 24px;color:var(--muted);font-size:15px}
label{display:block;font-size:14px;font-weight:500;margin:16px 0 6px}
input[type=email],input[type=password]{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:8px;
font:inherit;background:transparent;color:inherit}
input:focus-visible,button:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
button{font:inherit;cursor:pointer;border-radius:8px}
.primary{width:100%;margin-top:24px;padding:12px;border:0;background:var(--accent);color:#fff;font-weight:600}
.primary:hover{filter:brightness(1.08)}
.ghost{background:none;border:0;color:var(--muted);padding:8px 0;margin-top:10px;font-size:14px;text-decoration:underline}
.error{background:var(--err-bg);color:var(--err);border-radius:8px;padding:10px 12px;font-size:14px;margin:0 0 8px}
.note{margin-top:22px;padding-top:16px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}
.company{display:flex;gap:12px;align-items:center;border:1px solid var(--line);border-radius:10px;padding:12px 14px;
margin-top:10px;cursor:pointer;font-weight:500}
.company:has(input:checked){border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.company input{accent-color:var(--accent);width:18px;height:18px;margin:0}
.requester{border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:14px;margin:0 0 20px;color:var(--muted)}
.requester b{color:var(--ink);font-weight:600}
@media (prefers-reduced-motion:no-preference){.primary{transition:filter .15s}}
"""


_HEADERS: dict = {}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{escape(title)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600&display=swap">
<style>{_CSS}</style></head><body><main>
<div class="brand"><div class="mark" aria-hidden="true"><span></span></div>zOrder ERP <small>for Claude</small></div>
{body}</main></body></html>"""
    return HTMLResponse(html, status_code=status, headers=_HEADERS)


def _error(msg: str) -> str:
    return f'<p class="error" role="alert">{escape(msg)}</p>' if msg else ""


def _expired() -> HTMLResponse:
    return _page("Link expired", "<h1>This sign-in link has expired</h1>"
                 '<p class="lead">Go back to Claude and select Connect on the zOrder ERP connector again.</p>', 400)


class LoginPages:
    def __init__(self, store: Store, provider: ErpOAuthProvider, settings: Settings):
        self.store, self.provider, self.s = store, provider, settings
        _HEADERS.update(_headers(settings))
        self._failures: dict[str, list[float]] = {}  # email -> recent failure times (single worker)

    # ------------------------------------------------------------------ per-account throttle
    def _locked(self, email: str) -> bool:
        import time
        window = self.s.account_lockout_minutes * 60
        recent = [t for t in self._failures.get(email, []) if time.time() - t < window]
        self._failures[email] = recent
        return len(recent) >= self.s.account_max_failures

    def _fail(self, email: str) -> None:
        import time
        self._failures.setdefault(email, []).append(time.time())
        if len(self._failures) > 10000:  # bound memory
            self._failures = dict(list(self._failures.items())[-5000:])

    def _requester(self, pending: dict) -> str:
        """Who is asking: client name and where the login returns to (shown so admins can spot phishing)."""
        import json
        from urllib.parse import urlparse
        params = json.loads(pending["params_json"])
        host = urlparse(str(params.get("redirect_uri", ""))).netloc or "unknown"
        name = "Unknown app"
        raw = self.store.get_client(pending["client_id"])
        if raw:
            name = (json.loads(raw).get("client_name") or "Unnamed app")[:60]
        return (f'<p class="requester">Requested by <b>{escape(name)}</b><br>'
                f'Returns to <b>{escape(host)}</b></p>')

    # ------------------------------------------------------------------ rendering
    def _login_form(self, pending: dict, error: str = "", email: str = "") -> HTMLResponse:
        return _page("Connect zOrder ERP", f"""
<h1>Connect your ERP to Claude</h1>
<p class="lead">Sign in with your zOrder admin account. Claude gets read-only access to sales, stock,
customer and supplier reports.</p>
{self._requester(pending)}
{_error(error)}
<form method="post" action="{escape(self.s.base)}/login" autocomplete="on">
<input type="hidden" name="req" value="{escape(pending['id'])}">
<input type="hidden" name="csrf" value="{escape(pending['csrf'])}">
<label for="email">Email</label>
<input id="email" name="email" type="email" required autocomplete="username" value="{escape(email)}" autofocus>
<label for="password">Password</label>
<input id="password" name="password" type="password" required autocomplete="current-password">
<button class="primary" type="submit">Sign in and connect</button>
</form>
<form method="post" action="{escape(self.s.base)}/login/cancel">
<input type="hidden" name="req" value="{escape(pending['id'])}">
<input type="hidden" name="csrf" value="{escape(pending['csrf'])}">
<button class="ghost" type="submit">Cancel</button></form>
<p class="note">Your password goes only to zOrder. Claude never sees it, and nothing in the ERP can be changed
through this connection. Only continue if "Returns to" shows claude.ai or claude.com.</p>""", 401 if error else 200)

    def _company_form(self, pending: dict, instances: list[dict], error: str = "") -> HTMLResponse:
        items = "".join(
            f'<label class="company"><input type="radio" name="instance" value="{escape(i["id"])}"'
            f'{" checked" if n == 0 else ""}>{escape(i["name"])}</label>'
            for n, i in enumerate(instances))
        return _page("Choose a company", f"""
<h1>Choose a company</h1>
<p class="lead">Your account has access to {len(instances)} companies. Claude will read reports for the one you
choose. You can switch later by asking Claude.</p>
{self._requester(pending)}
{_error(error)}
<form method="post" action="{escape(self.s.base)}/login/company">
<input type="hidden" name="req" value="{escape(pending['id'])}">
<input type="hidden" name="csrf" value="{escape(pending['csrf'])}">
<fieldset style="border:0;padding:0;margin:0"><legend class="lead" style="margin:0">Companies</legend>{items}</fieldset>
<button class="primary" type="submit">Connect company</button></form>""")

    # ------------------------------------------------------------------ helpers
    async def _pending_from_form(self, request: Request) -> tuple[dict | None, dict]:
        form = await request.form()
        pending = self.store.get_pending(str(form.get("req", "")))
        if not pending or not hmac.compare_digest(pending["csrf"], str(form.get("csrf", ""))):
            return None, dict(form)
        return pending, dict(form)

    # ------------------------------------------------------------------ routes
    async def get_login(self, request: Request) -> Response:
        pending = self.store.get_pending(request.query_params.get("req", ""))
        if not pending:
            return _expired()
        if pending["session_id"]:
            session = self.store.get_session(pending["session_id"])
            if session:
                return self._company_form(pending, session.instances)
        return self._login_form(pending)

    async def post_login(self, request: Request) -> Response:
        pending, form = await self._pending_from_form(request)
        if not pending:
            return _expired()
        if pending["attempts"] >= self.s.max_login_attempts:
            self.store.delete_pending(pending["id"])
            return _page("Too many attempts", "<h1>Too many sign-in attempts</h1>"
                         '<p class="lead">For safety this link is now closed. Go back to Claude and connect again.</p>', 429)
        email = str(form.get("email", "")).strip()[:200]
        password = str(form.get("password", ""))
        if not email or not password:
            return self._login_form(pending, "Enter your email and password.", email)
        key = email.lower()
        if self._locked(key):
            return self._login_form(pending, f"Too many failed sign-in attempts for this account. Try again in "
                                             f"{self.s.account_lockout_minutes} minutes.", email)

        self.store.bump_attempts(pending["id"])
        try:
            result = await apifabric.login(self.s.apifabric_url, email, password, self.s.login_mode,
                                           self.s.erp_token_assumed_validity_days)
        except apifabric.LoginError as e:
            self._fail(key)
            log.info("login failed for a user (attempt %s)", pending["attempts"] + 1)
            return self._login_form(pending, str(e), email)
        self._failures.pop(key, None)

        session_id = self.store.create_session(result.user_id, result.token, result.instances, result.expires_at)
        log.info("ERP login ok: %d companies", len(result.instances))
        if len(result.instances) == 1:
            return RedirectResponse(self.provider.complete_login(pending, session_id), status_code=303)
        self.store.attach_session(pending["id"], session_id)
        return RedirectResponse(f"{self.s.base}/login?req={pending['id']}", status_code=303)

    async def post_company(self, request: Request) -> Response:
        pending, form = await self._pending_from_form(request)
        if not pending or not pending["session_id"]:
            return _expired()
        session = self.store.get_session(pending["session_id"])
        if not session:
            return _expired()
        choice = str(form.get("instance", ""))
        if not any(i["id"] == choice for i in session.instances):
            return self._company_form(pending, session.instances, "Choose one of the companies listed.")
        self.store.select_instance(session.id, choice)
        return RedirectResponse(self.provider.complete_login(pending, session.id), status_code=303)

    async def post_cancel(self, request: Request) -> Response:
        pending, _ = await self._pending_from_form(request)
        if not pending:
            return _expired()
        self.store.delete_pending(pending["id"])
        return RedirectResponse(self.provider.deny_redirect(pending), status_code=303)
