from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

from sre_rca_worker.integrations.mcp.models import DiscoveredTool


class McpClient(Protocol):
    endpoint_identity: str

    async def list_tools(self) -> tuple[DiscoveredTool, ...]: ...

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        deadline: datetime,
    ) -> bytes: ...


class EmptyMcpClient:
    endpoint_identity = "empty"

    async def list_tools(self) -> tuple[DiscoveredTool, ...]:
        return ()

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        deadline: datetime,
    ) -> bytes:
        raise RuntimeError("MCP is unavailable for this alert scope")


class LazyMcpClient:
    def __init__(
        self,
        *,
        endpoint_identity: str,
        endpoint: str,
        builder: Callable[[str], Awaitable[McpClient]],
    ) -> None:
        self.endpoint_identity = endpoint_identity
        self._endpoint = endpoint
        self._builder = builder
        self._client: McpClient | None = None

    async def _get(self) -> McpClient:
        if self._client is None:
            self._client = await self._builder(self._endpoint)
        return self._client

    async def list_tools(self) -> tuple[DiscoveredTool, ...]:
        return await (await self._get()).list_tools()

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        deadline: datetime,
    ) -> bytes:
        remaining = (deadline - datetime.now(deadline.tzinfo)).total_seconds()
        if remaining <= 0:
            raise TimeoutError("MCP deadline expired")
        async with asyncio.timeout(remaining):
            return await (await self._get()).call(tool_name, arguments, deadline)
