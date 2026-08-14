from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from sre_rca_worker.integrations.mcp.models import DiscoveredTool


class SdkMcpClient:
    """Google ADK/MCP adapter; intentionally owns all SDK imports."""

    def __init__(self, *, endpoint: str, headers: dict[str, str]) -> None:
        self.endpoint_identity = endpoint.rsplit("/", 1)[-1]
        self._endpoint = endpoint
        self._headers = headers

    @asynccontextmanager
    async def _session(self):  # type: ignore[no-untyped-def]
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            httpx2.AsyncClient(headers=self._headers) as http_client,
            streamable_http_client(self._endpoint, http_client=http_client) as streams,
        ):
            read_stream, write_stream = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    async def list_tools(self) -> tuple[DiscoveredTool, ...]:
        # Google ADK and MCP imports remain confined to this adapter boundary.
        from google.adk.tools.mcp_tool import McpToolset  # noqa: F401

        async with self._session() as session:
            result = await session.list_tools()
        return tuple(
            DiscoveredTool(
                name=tool.name,
                input_schema=tool.input_schema,
                annotations=(
                    tool.annotations.model_dump(exclude_none=True)
                    if tool.annotations is not None
                    else {}
                ),
            )
            for tool in result.tools
        )

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        deadline: datetime,
    ) -> bytes:
        async with self._session() as session:
            result = await session.call_tool(tool_name, arguments)
        return json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
