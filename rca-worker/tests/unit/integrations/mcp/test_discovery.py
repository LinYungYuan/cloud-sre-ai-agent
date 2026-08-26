from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from sre_rca_worker.integrations.mcp.discovery import discover_capabilities
from sre_rca_worker.integrations.mcp.factories import McpClientFactory
from sre_rca_worker.integrations.mcp.models import (
    CloudScope,
    DiscoveredTool,
    DiscoveryFailure,
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


@pytest.mark.asyncio
async def test_discovery_does_not_construct_specialists_without_required_manifest_entry(
    safe_scope: CloudScope,
) -> None:
    class RecordingFactory(FakeFactory):
        def __init__(self, clients):
            super().__init__(clients)
            self.constructed: list[SpecialistKind] = []

        def for_specialist(self, kind, scope):
            self.constructed.append(kind)
            return super().for_specialist(kind, scope)

    factory = RecordingFactory(
        {
            SpecialistKind.METRICS: FakeClient((_tool("metrics_query"),)),
            SpecialistKind.TRACE: FakeClient(()),
            SpecialistKind.LOG: FakeClient(()),
        }
    )

    capabilities, clients = await discover_capabilities(
        cast(McpClientFactory, factory),
        safe_scope,
        (),
        {
            SpecialistKind.METRICS: ("metrics.query",),
            SpecialistKind.TRACE: ("trace.query",),
            SpecialistKind.LOG: ("log.query",),
        },
        deadline=datetime.now(UTC) + timedelta(seconds=1),
    )

    assert capabilities.by_specialist == {}
    assert clients == {}
    assert factory.constructed == []


@pytest.mark.asyncio
async def test_discovery_keeps_two_successes_when_one_endpoint_times_out_in_fixed_order(
    safe_scope: CloudScope,
) -> None:
    class KindClient(FakeClient):
        def __init__(self, kind: SpecialistKind, failure: Exception | None = None):
            super().__init__(())
            self.endpoint_identity = kind.value
            self.failure = failure

        async def list_tools(self):
            self.list_tools_calls += 1
            if self.failure is not None:
                raise self.failure
            return (
                DiscoveredTool(
                    name=f"{self.endpoint_identity}_query",
                    input_schema=_manifest(
                        f"{self.endpoint_identity}.query",
                        endpoint_identity=self.endpoint_identity,
                    ).input_schema,
                    annotations={"readOnlyHint": True},
                ),
            )

    class OrderedFactory:
        def __init__(self):
            self.clients = {
                SpecialistKind.METRICS: KindClient(SpecialistKind.METRICS),
                SpecialistKind.TRACE: KindClient(
                    SpecialistKind.TRACE, TimeoutError("secret endpoint detail")
                ),
                SpecialistKind.LOG: KindClient(SpecialistKind.LOG),
            }

        def for_specialist(self, kind, scope):
            return self.clients[kind]

    factory = OrderedFactory()
    manifest = tuple(
        _manifest(f"{kind.value}.query", endpoint_identity=kind.value)
        for kind in SpecialistKind
    )
    capabilities, _ = await discover_capabilities(
        cast(McpClientFactory, factory),
        safe_scope,
        manifest,
        {kind: (f"{kind.value}.query",) for kind in SpecialistKind},
        deadline=datetime.now(UTC) + timedelta(seconds=1),
    )

    assert tuple(capabilities.by_specialist) == (
        SpecialistKind.METRICS,
        SpecialistKind.TRACE,
        SpecialistKind.LOG,
    )
    assert capabilities.for_specialist(SpecialistKind.METRICS)
    assert capabilities.for_specialist(SpecialistKind.TRACE) == ()
    assert capabilities.for_specialist(SpecialistKind.LOG)
    assert capabilities.discovery_failures == (
        DiscoveryFailure(specialist=SpecialistKind.TRACE, code="MCP_TIMEOUT"),
    )


@pytest.mark.asyncio
async def test_discovery_transport_failure_is_stable_and_does_not_block_other_endpoints(
    safe_scope: CloudScope,
) -> None:
    class Client:
        def __init__(self, kind: SpecialistKind, failure: Exception | None = None):
            self.endpoint_identity = kind.value
            self.failure = failure
            self.list_tools_calls = 0

        async def list_tools(self):
            self.list_tools_calls += 1
            if self.failure:
                raise self.failure
            entry = _manifest(
                f"{self.endpoint_identity}.query",
                endpoint_identity=self.endpoint_identity,
            )
            return (
                DiscoveredTool(
                    name=f"{self.endpoint_identity}_query",
                    input_schema=entry.input_schema,
                    annotations={"readOnlyHint": True},
                ),
            )

    clients = {
        SpecialistKind.METRICS: Client(SpecialistKind.METRICS, OSError("secret")),
        SpecialistKind.TRACE: Client(SpecialistKind.TRACE),
        SpecialistKind.LOG: Client(SpecialistKind.LOG),
    }

    class Factory:
        def for_specialist(self, kind, scope):
            return clients[kind]

    manifest = tuple(
        _manifest(f"{kind.value}.query", endpoint_identity=kind.value)
        for kind in SpecialistKind
    )
    capabilities, _ = await discover_capabilities(
        cast(McpClientFactory, Factory()),
        safe_scope,
        manifest,
        {kind: (f"{kind.value}.query",) for kind in SpecialistKind},
        deadline=datetime.now(UTC) + timedelta(seconds=1),
    )

    assert capabilities.for_specialist(SpecialistKind.METRICS) == ()
    assert capabilities.for_specialist(SpecialistKind.TRACE)
    assert capabilities.for_specialist(SpecialistKind.LOG)
    assert capabilities.discovery_failures == (
        DiscoveryFailure(specialist=SpecialistKind.METRICS, code="MCP_TRANSPORT"),
    )
