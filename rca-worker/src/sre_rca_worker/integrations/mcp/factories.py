from __future__ import annotations

from collections.abc import Awaitable, Callable

from sre_rca_worker.config.settings import WorkerSettings
from sre_rca_worker.integrations.mcp.client import (
    EmptyMcpClient,
    LazyMcpClient,
    McpClient,
)
from sre_rca_worker.integrations.mcp.models import CloudScope, SpecialistKind


async def _sdk_client_builder(endpoint: str) -> McpClient:
    # Imported only inside the Worker MCP adapter boundary. The concrete session
    # lifecycle is established at startup after tools/list capability discovery.
    from sre_rca_worker.integrations.mcp.sdk_client import SdkMcpClient

    return SdkMcpClient(endpoint=endpoint, headers={})


class McpClientFactory:
    def __init__(
        self,
        settings: WorkerSettings,
        *,
        transport_builder: Callable[[str], Awaitable[McpClient]] = _sdk_client_builder,
    ) -> None:
        self._transport_builder = transport_builder
        self._endpoints = {
            SpecialistKind.METRICS: settings.metrics_mcp_url,
            SpecialistKind.TRACE: settings.trace_mcp_url,
            SpecialistKind.LOG: settings.log_mcp_url,
        }

    def for_specialist(
        self, kind: SpecialistKind, scope: CloudScope | None
    ) -> McpClient:
        if scope is None or scope.provider != "GCP" or not scope.safe:
            return EmptyMcpClient()
        return LazyMcpClient(
            endpoint_identity=kind.value,
            endpoint=self._endpoints[kind],
            builder=self._transport_builder,
        )
