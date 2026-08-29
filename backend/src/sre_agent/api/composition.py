from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sre_agent.application.alerts.ingest_grafana_alerts import IngestGrafanaAlerts
from sre_agent.application.operator.read_models import (
    LocalOperatorIdentityProvider,
    OperatorIdentityProvider,
    OperatorReadService,
    UnavailableOperatorIdentityProvider,
    UnavailableOperatorReadService,
)
from sre_agent.application.outbox.publish_events import OutboxPublishService
from sre_agent.application.outbox.recover_events import (
    OutboxRecoveryService,
    SqlAlchemyOutboxRecoveryAuditRepository,
)
from sre_agent.config.settings import Settings
from sre_agent.integrations.grafana.authenticator import (
    ConfiguredGrafanaSecretProvider,
    GrafanaTokenAuthenticator,
)
from sre_agent.integrations.pubsub.publisher import (
    GooglePubSubPublisher,
    close_publisher_transport,
    create_publisher_client,
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
ReadinessCheck = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    uow_factory: UnitOfWorkFactory
    normalization_rule_provider: NormalizationRuleProvider
    folder_scope_provider: FolderScopeProvider
    readiness_check: ReadinessCheck
    operator_reads: OperatorReadService | None = None
    outbox_publish_service: OutboxPublishService | None = None
    outbox_recovery_service: OutboxRecoveryService | None = None


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    authenticator: GrafanaTokenAuthenticator
    ingestion: IngestGrafanaAlerts
    operator_reads: OperatorReadService
    operator_identity_provider: OperatorIdentityProvider
    outbox_recovery_service: OutboxRecoveryService | None
    readiness_check: ReadinessCheck


class ResourceFactory(Protocol):
    def __call__(
        self,
        settings: Settings,
    ) -> AbstractAsyncContextManager[RuntimeResources]: ...


@asynccontextmanager
async def production_resources(settings: Settings) -> AsyncIterator[RuntimeResources]:
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        publisher_client = create_publisher_client(settings.pubsub_emulator_host)
        try:
            topic = publisher_client.topic_path(
                settings.pubsub_project_id, settings.rca_topic_id
            )

            async def readiness_check() -> None:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))

            async with engine.connect() as connection:
                rules = await load_normalization_rule_provider(connection)
                folders = await load_folder_scope_provider(connection)
            _validate_configured_sources(settings, rules)
            outbox_publish_service = OutboxPublishService(
                session_factory,
                GooglePubSubPublisher(publisher_client),
                topic,
            )
            yield RuntimeResources(
                uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
                normalization_rule_provider=rules,
                folder_scope_provider=folders,
                readiness_check=readiness_check,
                operator_reads=SqlAlchemyOperatorReadRepository(session_factory),
                outbox_publish_service=outbox_publish_service,
                outbox_recovery_service=OutboxRecoveryService(
                    outbox_publish_service,
                    SqlAlchemyOutboxRecoveryAuditRepository(session_factory),
                ),
            )
        finally:
            try:
                publisher_client.stop()
            finally:
                close_publisher_transport(publisher_client)
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
            outbox_publish_service=resources.outbox_publish_service,
        ),
        operator_reads=operator_reads,
        operator_identity_provider=identity_provider,
        outbox_recovery_service=resources.outbox_recovery_service,
        readiness_check=resources.readiness_check,
    )


def _validate_configured_sources(
    settings: Settings,
    rules: NormalizationRuleProvider,
) -> None:
    missing = set(settings.grafana_tokens) - rules.source_ids
    if missing:
        raise ValueError("GRAFANA_TOKENS contains a source that is not enabled")
