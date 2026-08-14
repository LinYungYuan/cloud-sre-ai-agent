from datetime import UTC, datetime

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
