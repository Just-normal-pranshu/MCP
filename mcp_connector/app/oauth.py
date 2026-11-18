"""OAuth 2.1 authorization server for the connector (used by claude.ai custom connectors).

Flow: /authorize -> our login page (ERP email + password) -> company choice -> code -> /token.
Tokens issued to Claude are our own random tokens; the ERP token never leaves this server
except towards the Data API.
"""
from __future__ import annotations

import json
import time

from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import Settings
from .store import Store, new_secret

SCOPE = "erp.read"
CODE_TTL_SECONDS = 300


class ErpOAuthProvider:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.s = settings
        self._purge_disallowed_clients()

    def _purge_disallowed_clients(self) -> None:
        """Drop clients registered with redirect URIs the current policy no longer allows."""
        import json
        import logging
        for cid, raw in self.store.all_clients():
            uris = json.loads(raw).get("redirect_uris") or []
            if not uris or any(not self.s.redirect_allowed(str(u)) for u in uris):
                self.store.delete_client(cid)
                logging.getLogger("erp_mcp.oauth").warning("Removed OAuth client %s with a disallowed redirect URI", cid[:8])

    # ------------------------------------------------------------------ clients (dynamic registration)
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        bad = [str(u) for u in (client_info.redirect_uris or []) if not self.s.redirect_allowed(str(u))]
        if bad or not client_info.redirect_uris:
            raise RegistrationError(error="invalid_redirect_uri",
                                    error_description="This server only accepts Claude's OAuth callback URLs.")
        self.store.save_client(client_info.client_id, client_info.model_dump_json())

    # ------------------------------------------------------------------ authorize -> login page
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self.s.redirect_allowed(str(params.redirect_uri)):  # also covers clients registered earlier
            raise AuthorizeError("unauthorized_client", "Redirect URI is not allowed by this server.")
        if params.resource and params.resource.rstrip("/") != self.s.mcp_url.rstrip("/"):
            raise AuthorizeError("invalid_target", f"This server only issues tokens for {self.s.mcp_url}")
        self.store.purge_expired()
        pid, _ = self.store.create_pending(client.client_id, params.model_dump_json(), self.s.login_request_ttl_seconds)
        return f"{self.s.base}/login?req={pid}"

    def complete_login(self, pending: dict, session_id: str) -> str:
        """Called by the login page once the user and company are known. Returns the redirect to the client."""
        params = AuthorizationParams.model_validate_json(pending["params_json"])
        code = new_secret(32)
        self.store.save_code(code, {
            "client_id": pending["client_id"],
            "scopes": params.scopes or [SCOPE],
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource or self.s.mcp_url,
            "subject": session_id,
        }, time.time() + CODE_TTL_SECONDS)
        self.store.delete_pending(pending["id"])
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    @staticmethod
    def deny_redirect(pending: dict) -> str:
        params = AuthorizationParams.model_validate_json(pending["params_json"])
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied",
                                      error_description="User cancelled the connection.", state=params.state)

    # ------------------------------------------------------------------ codes
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        d = self.store.get_code(authorization_code)
        if not d or d["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code, scopes=d["scopes"], expires_at=d["expires_at"],
            client_id=d["client_id"], code_challenge=d["code_challenge"], redirect_uri=AnyUrl(d["redirect_uri"]),
            redirect_uri_provided_explicitly=d["redirect_uri_provided_explicitly"], resource=d["resource"],
            subject=d["subject"])

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        if self.store.get_code(authorization_code.code) is None:
            raise TokenError("invalid_grant", "Authorization code already used or expired.")
        self.store.delete_code(authorization_code.code)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject,
                           authorization_code.resource, family=new_secret(16))

    # ------------------------------------------------------------------ tokens
    def _issue(self, client_id: str, scopes: list[str], session_id: str | None, resource: str | None,
               family: str) -> OAuthToken:
        session = self.store.get_session(session_id) if session_id else None
        if session is None:
            raise TokenError("invalid_grant", "ERP session not found. Connect again.")
        now = time.time()
        if session.erp_expires_at <= now:
            raise TokenError("invalid_grant", "ERP session expired. Connect again.")
        access, refresh = new_secret(32), new_secret(32)
        access_exp = min(now + self.s.access_token_ttl_seconds, session.erp_expires_at)
        refresh_exp = min(now + self.s.refresh_token_ttl_days * 86400, session.erp_expires_at)
        self.store.save_token(access, "access", client_id, scopes, session.id, resource, family, access_exp)
        self.store.save_token(refresh, "refresh", client_id, scopes, session.id, resource, family, refresh_exp)
        return OAuthToken(access_token=access, expires_in=int(access_exp - now), scope=" ".join(scopes),
                          refresh_token=refresh)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        d = self.store.get_token(refresh_token, "refresh")
        if not d or d["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=d["client_id"], scopes=d["scopes"],
                            expires_at=int(d["expires_at"]), resource=d["resource"], subject=d["session_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        d = self.store.get_token(refresh_token.token, "refresh")
        if not d:
            raise TokenError("invalid_grant", "Refresh token expired or revoked.")
        self.store.revoke_family(d["family"])  # rotate: old access + refresh die together
        return self._issue(client.client_id, scopes or refresh_token.scopes, d["session_id"], d["resource"],
                           family=new_secret(16))

    async def load_access_token(self, token: str) -> AccessToken | None:
        d = self.store.get_token(token, "access")
        if not d:
            return None
        return AccessToken(token=token, client_id=d["client_id"], scopes=d["scopes"],
                           expires_at=int(d["expires_at"]), resource=d["resource"], subject=d["session_id"])

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        for kind in ("access", "refresh"):
            d = self.store.get_token(token.token, kind)
            if d:
                self.store.revoke_family(d["family"])
                return

    async def exchange_identity_assertion(self, client, params) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "Not supported.")
