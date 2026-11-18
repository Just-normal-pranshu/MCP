"""Calls the ERP Data API on behalf of the signed-in admin (ERP token + selected company)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from mcp.server.mcpserver.exceptions import ToolError

log = logging.getLogger("erp_mcp.data_client")

UNAVAILABLE_MSG = "Could not connect to server. Please contact the provider."

RECONNECT_MSG = ("The zOrder ERP session has expired or is no longer valid. Ask the user to open Claude "
                 "settings > Connectors, disconnect zOrder ERP and connect it again.")


class DataApiClient:
    def __init__(self, base_url: str, timeout: float):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def get(self, path: str, erp_token: str, instance_id: str, params: dict[str, Any] | None = None) -> dict:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        headers = {"Z-Auth-Token": erp_token, "Instance_Id": instance_id, "Instance-Id": instance_id}
        try:
            resp = await self._client.get(path, params=clean, headers=headers)
        except httpx.TimeoutException:
            raise ToolError("The ERP took too long to answer. Try a shorter date range or a single branch.")
        except httpx.HTTPError as e:
            log.error("Data API unreachable: %s", e.__class__.__name__)
            raise ToolError(UNAVAILABLE_MSG)

        try:
            body = resp.json()
        except ValueError:
            log.error("Data API returned non-JSON (HTTP %s): %s", resp.status_code, resp.text[:200].replace("\n", " "))
            raise ToolError(UNAVAILABLE_MSG)
        if resp.status_code == 401:
            raise ToolError(RECONNECT_MSG)
        if resp.status_code == 403:
            raise ToolError("This admin login has no data access for the active company in the ERP data service. "
                            "Switch to another company with erp_switch_company, or ask zOrder support to enable it.")
        if resp.status_code >= 400 or not body.get("success", False):
            raise ToolError(body.get("message") or f"ERP data request failed (HTTP {resp.status_code}).")
        return body

    async def aclose(self) -> None:
        await self._client.aclose()
