from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID

import httpx
import pytest

from sre_agent.api.composition import RuntimeResources
from sre_agent.api.main import create_app
from sre_agent.config.settings import Settings
from sre_agent.domain.alerts.classification import (
    AlertClassifier,
    ScopeField,
    ScopeResolver,
)
from sre_agent.persistence.repositories.alerts import (
    AlertRepository,
    SourceScope,
)
from sre_agent.persistence.repositories.incidents import IncidentRepository
from sre_agent.persistence.repositories.jobs import JobRepository
from sre_agent.persistence.repositories.normalization import (
    FolderScopeProvider,
    NormalizationRuleProvider,
)
from sre_agent.persistence.unit_of_work import UnitOfWork

SOURCE_ID = UUID("50000000-0000-0000-0000-000000000001")
DELIVERY_ID = UUID("60000000-0000-0000-0000-000000000001")
TEAM_ID = UUID("10000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("20000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
SERVICE_ID = UUID("40000000-0000-0000-0000-000000000001")
EXAMPLE = (
    Path(__file__).resolve().parents[4] / "contracts/examples/grafana-firing.json"
).read_bytes()


class KnownScope(ScopeResolver):
    def resolve(self, field: ScopeField, label_value: str) -> UUID | None:
        return {
            ("team", "platform"): TEAM_ID,
            ("project", "svc-lx-afa-01-uat-1b9a87"): PROJECT_ID,
            ("environment", "uat"): ENVIRONMENT_ID,
            ("service", "aaaa"): SERVICE_ID,
        }.get((field, label_value))


class ClassifierProvider:
    def __init__(self) -> None:
        self.sources: list[UUID] = []

    def for_source(self, source_id: UUID) -> AlertClassifier:
        self.sources.append(source_id)
        return AlertClassifier(source_id, KnownScope(), [])


class FakeAlertRepository:
    def __init__(self) -> None:
        self.delivery: dict[str, Any] | None = None
        self.finished_status: str | None = None

    async def get_source_scope(self, source_id: UUID) -> SourceScope:
        assert source_id == SOURCE_ID
        return SourceScope(TEAM_ID, PROJECT_ID, ENVIRONMENT_ID)

    async def create_delivery(self, **values: Any) -> UUID:
        self.delivery = values
        return DELIVERY_ID

    async def claim_dedup_key(self, **values: Any) -> bool:
        del values
        return False

    async def add_event(self, **values: Any):
        raise AssertionError(f"duplicate test must not add an event: {values}")

    async def upsert_instance(self, **values: Any) -> None:
        raise AssertionError(f"duplicate test must not add an instance: {values}")

    async def finish_delivery(self, **values: Any) -> None:
        self.finished_status = values["status"]


class UnusedRepository:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"duplicate test must not call {name}")


class FakeUnitOfWork(UnitOfWork):
    def __init__(self) -> None:
        self.alert_repository = FakeAlertRepository()
        self.alerts = cast(AlertRepository, self.alert_repository)
        self.incidents = cast(IncidentRepository, UnusedRepository())
        self.jobs = cast(JobRepository, UnusedRepository())
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        self.committed = exc_type is None


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


@pytest.mark.asyncio
async def test_configured_app_accepts_valid_webhook_without_dependency_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch)
    uow = FakeUnitOfWork()
    classifiers = ClassifierProvider()
    observed_settings: list[Settings] = []

    async def readiness_check() -> None:
        return None

    @asynccontextmanager
    async def resources(settings: Settings) -> AsyncIterator[RuntimeResources]:
        observed_settings.append(settings)
        yield RuntimeResources(
            uow_factory=lambda: uow,
            normalization_rule_provider=NormalizationRuleProvider({}, frozenset()),
            folder_scope_provider=FolderScopeProvider({}),
            readiness_check=readiness_check,
        )

    app = create_app(resource_factory=resources)
    assert app.dependency_overrides == {}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(
            app=app,  # pyright: ignore[reportArgumentType]
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://configured.test",
        ) as client:
            response = await client.post(
                f"/webhooks/v1/grafana/{SOURCE_ID}",
                content=EXAMPLE,
                headers={
                    "Authorization": "Bearer accepted-token",
                    "Content-Type": "application/json",
                },
            )

    assert response.status_code == 202
    assert response.json()["deliveryId"] == str(DELIVERY_ID)
    assert len(observed_settings) == 1
    assert classifiers.sources == []
    assert uow.committed
    assert uow.alert_repository.finished_status == "DUPLICATE"
    assert uow.alert_repository.delivery is not None
    assert uow.alert_repository.delivery["raw_body"] == EXAMPLE
    assert uow.alert_repository.delivery["token_id"] == "current-2026-08"
    assert "accepted-token" not in repr(observed_settings[0])


@pytest.mark.asyncio
async def test_missing_configuration_fails_lifespan_startup(monkeypatch) -> None:
    for key in (
        "DATABASE_URL",
        "GRAFANA_TOKENS",
        "PUBSUB_PROJECT_ID",
        "RCA_TOPIC_ID",
        "APP_ENVIRONMENT",
        "MODEL_NAME",
        "METRICS_MCP_URL",
        "TRACE_MCP_URL",
        "LOG_MCP_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    app = create_app()

    with pytest.raises(Exception, match="validation"):
        async with app.router.lifespan_context(app):
            raise AssertionError("invalid configuration must not start the app")
