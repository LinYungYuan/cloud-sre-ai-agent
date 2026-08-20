from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from sre_agent.api.composition import RuntimeResources
from sre_agent.api.main import create_app
from sre_agent.config.settings import Settings
from sre_agent.persistence.repositories.normalization import (
    FolderScopeProvider,
    NormalizationRuleProvider,
)
from sre_agent.persistence.unit_of_work import UnitOfWork


def _set_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "DATABASE_URL": "postgresql+asyncpg://app:do-not-leak@db/sre",
        "GRAFANA_TOKENS": (
            '{"50000000-0000-0000-0000-000000000001":'
            '{"current-2026-08":"accepted-token"}}'
        ),
        "PUBSUB_PROJECT_ID": "local-project",
        "RCA_TOPIC_ID": "rca-jobs",
        "APP_ENVIRONMENT": "test",
        "MODEL_NAME": "test-model",
        "METRICS_MCP_URL": "https://gateway/gcp/metrics/mcp",
        "TRACE_MCP_URL": "https://gateway/gcp/trace/mcp",
        "LOG_MCP_URL": "https://gateway/gcp/log/mcp",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _resource_factory(readiness_check):
    def unused_uow_factory() -> UnitOfWork:
        raise AssertionError("health probes must not open a unit of work")

    @asynccontextmanager
    async def resources(settings: Settings) -> AsyncIterator[RuntimeResources]:
        del settings
        yield RuntimeResources(
            uow_factory=unused_uow_factory,
            normalization_rule_provider=NormalizationRuleProvider({}, frozenset()),
            folder_scope_provider=FolderScopeProvider({}),
            readiness_check=readiness_check,
        )

    return resources


async def _get(app, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(
        app=app,  # pyright: ignore[reportArgumentType]
        raise_app_exceptions=False,
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://health.test",
    ) as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_live_returns_ok_without_invoking_readiness_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch)
    invocations = 0

    async def failing_check() -> None:
        nonlocal invocations
        invocations += 1
        raise RuntimeError("postgresql://secret")

    app = create_app(resource_factory=_resource_factory(failing_check))

    async with app.router.lifespan_context(app):
        response = await _get(app, "/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert invocations == 0


@pytest.mark.asyncio
async def test_ready_returns_ok_when_readiness_check_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch)

    async def successful_check() -> None:
        return None

    app = create_app(resource_factory=_resource_factory(successful_check))

    async with app.router.lifespan_context(app):
        response = await _get(app, "/health/ready")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_ready_hides_readiness_check_exception_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch)

    async def failing_check() -> None:
        raise RuntimeError("postgresql://secret")

    app = create_app(resource_factory=_resource_factory(failing_check))

    async with app.router.lifespan_context(app):
        response = await _get(app, "/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "service unavailable"}
    assert "postgresql://secret" not in response.text


@pytest.mark.asyncio
async def test_ready_returns_unavailable_when_readiness_check_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch)

    async def stalled_check() -> None:
        await asyncio.Event().wait()

    app = create_app(resource_factory=_resource_factory(stalled_check))

    async with app.router.lifespan_context(app):
        response = await _get(app, "/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "service unavailable"}
