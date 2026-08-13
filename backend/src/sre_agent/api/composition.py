from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sre_agent.application.alerts.ingest_grafana_alerts import (
    ClassifierProvider,
    IngestGrafanaAlerts,
)
from sre_agent.config.settings import Settings
from sre_agent.integrations.grafana.authenticator import (
    ConfiguredGrafanaSecretProvider,
    GrafanaTokenAuthenticator,
)
from sre_agent.persistence.repositories.classification import (
    LoadedClassifierProvider,
    load_classifier_provider,
)
from sre_agent.persistence.unit_of_work import SqlAlchemyUnitOfWork, UnitOfWork

UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    uow_factory: UnitOfWorkFactory
    classifier_provider: ClassifierProvider


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    authenticator: GrafanaTokenAuthenticator
    ingestion: IngestGrafanaAlerts


class ResourceFactory(Protocol):
    def __call__(
        self,
        settings: Settings,
    ) -> AbstractAsyncContextManager[RuntimeResources]: ...


@asynccontextmanager
async def production_resources(settings: Settings) -> AsyncIterator[RuntimeResources]:
    engine = create_async_engine(settings.database_url.get_secret_value())
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.connect() as connection:
            classifiers = await load_classifier_provider(connection)
        _validate_configured_sources(settings, classifiers)
        yield RuntimeResources(
            uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
            classifier_provider=classifiers,
        )
    finally:
        await engine.dispose()


def compose_services(
    settings: Settings,
    resources: RuntimeResources,
) -> ApplicationServices:
    secret_provider = ConfiguredGrafanaSecretProvider(settings.grafana_tokens)
    return ApplicationServices(
        authenticator=GrafanaTokenAuthenticator(secret_provider),
        ingestion=IngestGrafanaAlerts(
            uow_factory=resources.uow_factory,
            classifier_provider=resources.classifier_provider,
            max_body_bytes=settings.webhook_max_body_bytes,
        ),
    )


def _validate_configured_sources(
    settings: Settings,
    classifiers: LoadedClassifierProvider,
) -> None:
    missing = set(settings.grafana_tokens) - classifiers.source_ids
    if missing:
        raise ValueError("GRAFANA_TOKENS contains a source that is not enabled")
