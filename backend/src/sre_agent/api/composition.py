from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sre_agent.application.alerts.ingest_grafana_alerts import IngestGrafanaAlerts
from sre_agent.application.operator.read_models import (
    LocalOperatorIdentityProvider,
    OperatorIdentityProvider,
    OperatorReadService,
    UnavailableOperatorIdentityProvider,
    UnavailableOperatorReadService,
)
from sre_agent.config.settings import Settings
from sre_agent.integrations.grafana.authenticator import (
    ConfiguredGrafanaSecretProvider,
    GrafanaTokenAuthenticator,
)
from sre_agent.persistence.repositories.normalization import (
    FolderScopeProvider,
    NormalizationRuleProvider,
    load_folder_scope_provider,
    load_normalization_rule_provider,
)
from sre_agent.persistence.repositories.operator_reads import (
    SqlAlchemyOperatorReadRepository,
)
from sre_agent.persistence.unit_of_work import SqlAlchemyUnitOfWork, UnitOfWork

UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    uow_factory: UnitOfWorkFactory
    normalization_rule_provider: NormalizationRuleProvider
    folder_scope_provider: FolderScopeProvider
    operator_reads: OperatorReadService | None = None


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    authenticator: GrafanaTokenAuthenticator
    ingestion: IngestGrafanaAlerts
    operator_reads: OperatorReadService
    operator_identity_provider: OperatorIdentityProvider


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
            rules = await load_normalization_rule_provider(connection)
            folders = await load_folder_scope_provider(connection)
        _validate_configured_sources(settings, rules)
        yield RuntimeResources(
            uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
            normalization_rule_provider=rules,
            folder_scope_provider=folders,
            operator_reads=SqlAlchemyOperatorReadRepository(session_factory),
        )
    finally:
        await engine.dispose()


def compose_services(
    settings: Settings,
    resources: RuntimeResources,
) -> ApplicationServices:
    secret_provider = ConfiguredGrafanaSecretProvider(settings.grafana_tokens)
    operator_reads = resources.operator_reads
    if operator_reads is None:
        operator_reads = UnavailableOperatorReadService()
    identity_provider: OperatorIdentityProvider
    if settings.app_environment == "local":
        identity_provider = LocalOperatorIdentityProvider(
            app_environment=settings.app_environment
        )
    else:
        identity_provider = UnavailableOperatorIdentityProvider()
    return ApplicationServices(
        authenticator=GrafanaTokenAuthenticator(secret_provider),
        ingestion=IngestGrafanaAlerts(
            uow_factory=resources.uow_factory,
            normalization_rule_provider=resources.normalization_rule_provider,
            folder_scope_provider=resources.folder_scope_provider,
            max_body_bytes=settings.webhook_max_body_bytes,
        ),
        operator_reads=operator_reads,
        operator_identity_provider=identity_provider,
    )


def _validate_configured_sources(
    settings: Settings,
    rules: NormalizationRuleProvider,
) -> None:
    missing = set(settings.grafana_tokens) - rules.source_ids
    if missing:
        raise ValueError("GRAFANA_TOKENS contains a source that is not enabled")
