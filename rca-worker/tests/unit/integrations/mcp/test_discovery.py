from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

import pytest

from sre_rca_worker.integrations.mcp.discovery import discover_capabilities
from sre_rca_worker.integrations.mcp.factories import McpClientFactory
from sre_rca_worker.integrations.mcp.models import (
    CloudScope,
    DiscoveredTool,
    ManifestEntry,
    SpecialistKind,
)


def _manifest(capability: str, *, endpoint_identity: str = "metrics") -> ManifestEntry:
    return ManifestEntry.model_validate(
        {
            "endpoint_identity": endpoint_identity,
            "capability": capability,
            "tool_name_pattern": rf"^{capability.replace('.', '_')}$",
            "input_schema": {
                "type": "object",
                "properties": {"scope_id": {"type": "string"}},
                "required": ["scope_id"],
                "additionalProperties": False,
            },
            "risk": "READ_ONLY",
        }
    )


class FakeClient:
    endpoint_identity = "metrics"

    def __init__(self, tools: tuple[DiscoveredTool, ...]) -> None:
        self._tools = tools
        self.list_tools_calls = 0

    async def list_tools(self) -> tuple[DiscoveredTool, ...]:
        self.list_tools_calls += 1
        return self._tools

    async def call(
        self, tool_name: str, arguments: dict[str, Any], deadline: datetime
    ) -> bytes:
        raise AssertionError("discovery must not call MCP tools")


class FakeFactory:
    def __init__(self, clients: Mapping[SpecialistKind, FakeClient]) -> None:
        self._clients = clients

    def for_specialist(
        self, kind: SpecialistKind, scope: CloudScope | None
    ) -> FakeClient:
        return self._clients[kind]


def _tool(name: str) -> DiscoveredTool:
    return DiscoveredTool.model_validate(
        {
            "name": name,
            "input_schema": _manifest("metrics.query").input_schema,
            "annotations": {"readOnlyHint": True},
        }
    )


@pytest.fixture
def safe_scope() -> CloudScope:
    return CloudScope(provider="GCP", scope_id="project-a", safe=True)


@pytest.mark.asyncio
async def test_discovery_exposes_only_capabilities_required_by_skill(
    safe_scope: CloudScope,
) -> None:
    fake_client = FakeClient((_tool("metrics_query"), _tool("metrics_unused")))
    factory = FakeFactory(
        {
            SpecialistKind.METRICS: fake_client,
            SpecialistKind.TRACE: FakeClient(()),
            SpecialistKind.LOG: FakeClient(()),
        }
    )
    manifest = (_manifest("metrics.query"), _manifest("metrics.unused"))

    capabilities, _ = await discover_capabilities(
        cast(McpClientFactory, factory),
        safe_scope,
        manifest,
        {SpecialistKind.METRICS: ("metrics.query",)},
    )

    assert [
        tool.capability for tool in capabilities.for_specialist(SpecialistKind.METRICS)
    ] == ["metrics.query"]
    assert fake_client.list_tools_calls == 1


@pytest.mark.asyncio
async def test_discovery_leaves_specialist_empty_when_required_capability_has_no_exact_match(
    safe_scope: CloudScope,
) -> None:
    fake_client = FakeClient((_tool("metrics_unrelated"),))
    factory = FakeFactory(
        {
            SpecialistKind.METRICS: fake_client,
            SpecialistKind.TRACE: FakeClient(()),
            SpecialistKind.LOG: FakeClient(()),
        }
    )
    manifest = (
        _manifest("metrics.query", endpoint_identity="metrics"),
        _manifest("metrics.unrelated", endpoint_identity="metrics"),
    )

    capabilities, _ = await discover_capabilities(
        cast(McpClientFactory, factory),
        safe_scope,
        manifest,
        {SpecialistKind.METRICS: ("metrics.query",)},
    )

    assert capabilities.for_specialist(SpecialistKind.METRICS) == ()
