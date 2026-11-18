"""Settings for the zOrder ERP MCP connector. Environment variables / .env with prefix ERP_MCP_."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ERP_MCP_", extra="ignore")

    # Public HTTPS URL where this service is reachable (nginx strips the path prefix before proxying).
    public_base_url: str = "https://aiagent.zorder.in/erp-mcp"

    # APIFabric base URL: POST {apifabric_url}/access_token
    apifabric_url: str = "http://localhost:5000"
    login_mode: int = 1  # 1 = admin profile

    # ERP Data API base URL (the REST service over the stored procedures)
    data_api_url: str = "http://127.0.0.1:8820"
    data_api_timeout_seconds: float = 150

    # SQLite file for OAuth clients, tokens and ERP sessions
    db_path: str = "erp_mcp.sqlite3"
    # Fernet key used to encrypt ERP tokens at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = ""

    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_days: int = 30
    erp_token_assumed_validity_days: int = 7  # used when the login response carries no TokenValidity
    login_request_ttl_seconds: int = 600
    max_login_attempts: int = 5

    # OAuth redirect URIs a client may register (exact match, comma separated). Only Claude's callbacks by
    # default, so nobody can register their own redirect and phish an admin's login.
    allowed_redirect_uris: str = ("https://claude.ai/api/mcp/auth_callback,"
                                  "https://claude.com/api/mcp/auth_callback")
    # Also allow http://localhost / 127.0.0.1 callbacks (Claude Code, mcp-remote). Local testing only.
    allow_localhost_redirects: bool = False

    # Per-account login throttle (on top of nginx per-IP limits)
    account_max_failures: int = 5
    account_lockout_minutes: int = 15

    # Host headers accepted by the MCP endpoint (DNS-rebinding protection), comma separated
    allowed_hosts: str = "aiagent.zorder.in"

    host: str = "127.0.0.1"
    port: int = 8810
    log_level: str = "INFO"

    @property
    def base(self) -> str:
        return self.public_base_url.rstrip("/")

    @property
    def mcp_url(self) -> str:
        return f"{self.base}/mcp"

    @property
    def redirect_allowlist(self) -> list[str]:
        return [u.strip() for u in self.allowed_redirect_uris.split(",") if u.strip()]

    def redirect_allowed(self, uri: str) -> bool:
        from urllib.parse import urlparse
        if uri in self.redirect_allowlist:
            return True
        if self.allow_localhost_redirects:
            u = urlparse(uri)
            return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
