"""zOrder ERP MCP connector: remote MCP server (Streamable HTTP) with its own OAuth login.

Run:  uvicorn app.main:app --host 127.0.0.1 --port 8810
"""
from __future__ import annotations

import logging

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import get_settings
from .data_client import DataApiClient
from .oauth import SCOPE, ErpOAuthProvider
from .store import Store
from .tools import INSTRUCTIONS, register_tools
from .web import LoginPages

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

store = Store(settings.db_path, settings.encryption_key)
provider = ErpOAuthProvider(store, settings)
data_api = DataApiClient(settings.data_api_url, settings.data_api_timeout_seconds)

mcp = MCPServer(
    name="zOrder ERP",
    instructions=INSTRUCTIONS,
    version="1.0.0",
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=settings.base,
        resource_server_url=settings.mcp_url,
        validate_token_resource=True,
        required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE],
                                                              default_scopes=[SCOPE]),
        revocation_options=RevocationOptions(enabled=True),
    ),
)
register_tools(mcp, store, data_api)

pages = LoginPages(store, provider, settings)
mcp.custom_route("/login", methods=["GET"])(pages.get_login)
mcp.custom_route("/login", methods=["POST"])(pages.post_login)
mcp.custom_route("/login/company", methods=["POST"])(pages.post_company)
mcp.custom_route("/login/cancel", methods=["POST"])(pages.post_cancel)


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


hosts = [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()]
app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    host=settings.host,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts + [f"{h}:*" for h in hosts] + ["127.0.0.1:*", "localhost:*"],
        allowed_origins=[f"https://{h}" for h in hosts] + ["https://claude.ai"],
    ),
)
