from datetime import UTC, datetime, timedelta

import pytest

from sre_rca_worker.config.settings import WorkerSettings
from sre_rca_worker.integrations.mcp.factories import McpClientFactory
from sre_rca_worker.integrations.mcp.models import CloudScope, SpecialistKind


def _settings() -> WorkerSettings:
    return WorkerSettings.model_validate(
        {
            "database_url": "postgresql+asyncpg://postgres@localhost/sre_agent",
            "pubsub_project_id": "local",
            "rca_topic_id": "rca",
            "pubsub_subscription_id": "rca-worker",
            "app_environment": "test",
            "model_name": "test-model",
        }
    )


def test_default_mcp_endpoints_are_exact_and_have_no_authentication() -> None:
    settings = _settings()
    assert str(settings.metrics_mcp_url) == (
        "https://agentgateway.cp.gcubut.gcp.uwccb/agw/gcp-metrics-mcp"
    )
    assert str(settings.trace_mcp_url) == (
        "https://agentgateway.cp.gcubut.gcp.uwccb/agw/gcp-trace-mcp"
    )
    assert str(settings.log_mcp_url) == (
        "https://agentgateway.cp.gcubut.gcp.uwccb/agw/gcp-log-mcp"
    )
    assert settings.mcp_headers == {}


@pytest.mark.asyncio
async def test_aws_or_missing_scope_never_builds_or_connects_gcp_client() -> None:
    built: list[str] = []

    async def transport_builder(endpoint: str):  # type: ignore[no-untyped-def]
        built.append(endpoint)
        raise AssertionError("must not connect")

    factory = McpClientFactory(_settings(), transport_builder=transport_builder)
    aws = CloudScope(provider="AWS", scope_id="123456789012", safe=True)
    unsafe = CloudScope(provider="GCP", scope_id="project-a", safe=False)

    for scope in (aws, unsafe, None):
        client = factory.for_specialist(SpecialistKind.METRICS, scope)
        assert await client.list_tools() == ()
    assert built == []


@pytest.mark.asyncio
async def test_each_specialist_uses_only_its_configured_endpoint_for_list_and_call() -> (
    None
):
    settings = WorkerSettings.model_validate(
        {
            "database_url": "postgresql+asyncpg://postgres@localhost/sre_agent",
            "pubsub_project_id": "local",
            "rca_topic_id": "rca",
            "pubsub_subscription_id": "rca-worker",
            "app_environment": "test",
            "model_name": "test-model",
            "metrics_mcp_url": "https://metrics.example.test/query",
            "trace_mcp_url": "https://trace.example.test/query",
            "log_mcp_url": "https://logs.example.test/query",
        }
    )
    built: list[str] = []
    calls: list[tuple[str, str, dict[str, object]]] = []

    class Client:
        def __init__(self, endpoint: str) -> None:
            self.endpoint = endpoint
            self.endpoint_identity = endpoint.rsplit("/", 2)[-2]

        async def list_tools(self):  # type: ignore[no-untyped-def]
            calls.append((self.endpoint, "list", {}))
            return ()

        async def call(
            self,
            tool_name: str,
            arguments: dict[str, object],
            deadline: datetime,
        ) -> bytes:
            del deadline
            calls.append((self.endpoint, tool_name, arguments))
            return b"{}"

    async def transport_builder(endpoint: str) -> Client:
        built.append(endpoint)
        return Client(endpoint)

    factory = McpClientFactory(settings, transport_builder=transport_builder)
    scope = CloudScope(provider="GCP", scope_id="project-a", safe=True)
    deadline = datetime.now(UTC)
    deadline = deadline.replace(microsecond=0)
    deadline = deadline + timedelta(seconds=30)
    configured = {
        SpecialistKind.METRICS: str(settings.metrics_mcp_url),
        SpecialistKind.TRACE: str(settings.trace_mcp_url),
        SpecialistKind.LOG: str(settings.log_mcp_url),
    }

    for kind in SpecialistKind:
        client = factory.for_specialist(kind, scope)
        assert client.endpoint_identity == kind.value
        assert await client.list_tools() == ()
        assert (
            await client.call(
                f"{kind.value}_query",
                {"project_id": "project-a"},
                deadline,
            )
            == b"{}"
        )

    assert built == [configured[kind] for kind in SpecialistKind]
    assert calls == [
        item
        for kind in SpecialistKind
        for item in (
            (configured[kind], "list", {}),
            (configured[kind], f"{kind.value}_query", {"project_id": "project-a"}),
        )
    ]


def test_factory_never_accepts_job_endpoint_or_tool_override() -> None:
    factory = McpClientFactory(_settings())
    scope = CloudScope(provider="GCP", scope_id="project-a", safe=True)
    client = factory.for_specialist(SpecialistKind.METRICS, scope)

    assert client.endpoint_identity == "metrics"
    assert "evil" not in repr(client)
    with pytest.raises(TypeError):
        factory.for_specialist(  # pyright: ignore[reportCallIssue]
            SpecialistKind.METRICS,
            scope,
            endpoint="https://evil.test",  # pyright: ignore[reportCallIssue]
        )
    assert datetime.now(UTC).tzinfo is UTC
