from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Protocol
from uuid import UUID

from sre_agent.application.outbox.publish_events import (
    OutboxPublishResult,
    PublishResultCode,
)
from sre_agent.domain.alerts.fingerprint import hash_raw_body
from sre_agent.domain.alerts.identity import make_incident_identity_v2
from sre_agent.domain.alerts.models import AlertState
from sre_agent.domain.alerts.normalization import SafeRuleEngine
from sre_agent.domain.alerts.provider import detect_provider
from sre_agent.domain.common import require_aware_utc
from sre_agent.integrations.grafana.normalizer import (
    CanonicalAlertEvent,
    normalize_alerts,
)
from sre_agent.integrations.grafana.payloads import parse_grafana_body
from sre_agent.persistence.repositories.incidents import IncidentScope
from sre_agent.persistence.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)


class NormalizationRuleProvider(Protocol):
    def for_source(self, source_id: UUID) -> SafeRuleEngine: ...


class FolderScopeProvider(Protocol):
    def resolve(self, source_id: UUID, folder_code: str | None) -> IncidentScope: ...


class OutboxEventPublisher(Protocol):
    async def publish_event(self, event_id: UUID) -> OutboxPublishResult: ...


class StaticClassifierProvider:
    """Deprecated compatibility adapter; legacy classifiers are not consulted."""

    def __init__(self, classifier: object) -> None:
        self.classifier = classifier


class _EmptyRuleProvider:
    def for_source(self, source_id: UUID) -> SafeRuleEngine:
        del source_id
        return SafeRuleEngine(())


class _EmptyFolderScopeProvider:
    def resolve(self, source_id: UUID, folder_code: str | None) -> IncidentScope:
        del source_id, folder_code
        return IncidentScope()


@dataclass(frozen=True, slots=True)
class IngestionResult:
    delivery_id: UUID
    accepted_at: datetime
    incident_ids: tuple[UUID, ...]
    outbox_event_ids: tuple[UUID, ...] = ()


class IngestGrafanaAlerts:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        max_body_bytes: int,
        normalization_rule_provider: NormalizationRuleProvider | None = None,
        folder_scope_provider: FolderScopeProvider | None = None,
        classifier_provider: object | None = None,
        outbox_publish_service: OutboxEventPublisher | None = None,
    ) -> None:
        del classifier_provider
        self._uow_factory = uow_factory
        self._normalization_rule_provider = (
            normalization_rule_provider or _EmptyRuleProvider()
        )
        self._folder_scope_provider = (
            folder_scope_provider or _EmptyFolderScopeProvider()
        )
        self._max_body_bytes = max_body_bytes
        self._outbox_publish_service = outbox_publish_service

    async def execute(
        self,
        source_id: UUID,
        token_id: str,
        raw_body: bytes,
        received_at: datetime,
    ) -> IngestionResult:
        accepted_at = require_aware_utc(received_at)
        webhook = parse_grafana_body(raw_body, self._max_body_bytes)
        canonical_events = normalize_alerts(
            source_id,
            webhook,
            self._normalization_rule_provider.for_source(source_id),
        )
        raw_document = json.loads(raw_body)
        raw_alerts = raw_document["alerts"]
        if not isinstance(raw_alerts, list) or len(raw_alerts) != len(canonical_events):
            raise ValueError("Grafana raw alert list does not match normalized events")

        incident_ids: list[UUID] = []
        outbox_event_ids: list[UUID] = []
        async with self._uow_factory() as uow:
            body_hash = hash_raw_body(raw_body)
            delivery_id = await uow.alerts.create_delivery(
                source_id=source_id,
                token_id=token_id,
                received_at=accepted_at,
                body_hash=body_hash,
                raw_body=raw_body,
                raw_payload=raw_document,
                truncated_alerts=webhook.truncated_alerts or 0,
                incomplete=bool(webhook.truncated_alerts),
            )

            new_event_count = 0
            has_invalid_alert = False
            for event, raw_alert in zip(canonical_events, raw_alerts, strict=True):
                provider = detect_provider(event.labels)
                identity = make_incident_identity_v2(
                    source_id,
                    event.folder_code,
                    event.alert_name,
                    event.fingerprint,
                )
                validation_errors = list(provider.errors + identity.errors)
                claimed = await uow.alerts.claim_dedup_key(
                    source_id=source_id,
                    dedup_key=event.dedup_key,
                    delivery_id=delivery_id,
                )
                if not claimed:
                    continue
                new_event_count += 1
                if validation_errors:
                    has_invalid_alert = True
                stored_event = await uow.alerts.add_event(
                    delivery_id=delivery_id,
                    received_at=accepted_at,
                    event=event,
                    raw_payload=raw_alert,
                    validation_status=(
                        "VALIDATION_FAILED" if validation_errors else "VALID"
                    ),
                    validation_errors=validation_errors,
                )
                await uow.alerts.upsert_instance(
                    event=event,
                    stored_event=stored_event,
                    received_at=accepted_at,
                )

                incident_scope = self._folder_scope_provider.resolve(
                    source_id, event.folder_code
                )
                incident_id: UUID | None
                if event.status is AlertState.FIRING:
                    reopened_from = await uow.incidents.latest_resolved(
                        identity.key, identity.version
                    )
                    selection = await uow.incidents.get_or_create_active(
                        identity_key=identity.key,
                        identity_version=identity.version,
                        provider=event.provider.value,
                        folder_code=event.folder_code,
                        alert_name=event.alert_name,
                        scope=incident_scope,
                        title=_title(event),
                        severity=_severity(event),
                        opened_at=accepted_at,
                        reopened_from_incident_id=reopened_from,
                    )
                    incident_id = selection.id
                    await uow.incidents.link_alert(incident_id, stored_event)
                    if not selection.created:
                        await uow.incidents.set_alert_state(
                            incident_id, AlertState.FIRING.value, accepted_at
                        )
                    else:
                        rca_work = await uow.jobs.create_rca_work(
                            incident_id=incident_id,
                            run_status="QUEUED",
                            available_at=accepted_at,
                        )
                        if rca_work.outbox_event_id is not None:
                            outbox_event_ids.append(rca_work.outbox_event_id)
                else:
                    incident_id = await uow.incidents.lock_latest(
                        identity.key, identity.version
                    )
                    if incident_id is not None:
                        await uow.incidents.link_alert(incident_id, stored_event)
                        await uow.incidents.set_alert_state(
                            incident_id, AlertState.RESOLVED.value, accepted_at
                        )

                if incident_id is not None and incident_id not in incident_ids:
                    incident_ids.append(incident_id)

            await uow.alerts.finish_delivery(
                delivery_id=delivery_id,
                status=(
                    "VALIDATION_FAILED"
                    if has_invalid_alert
                    else ("PROCESSED" if new_event_count else "DUPLICATE")
                ),
                processed_at=accepted_at,
            )

        result = IngestionResult(
            delivery_id,
            accepted_at,
            tuple(incident_ids),
            tuple(outbox_event_ids),
        )
        await self._publish_new_events(result)
        return result

    async def _publish_new_events(self, result: IngestionResult) -> None:
        if self._outbox_publish_service is None:
            return
        for event_id in result.outbox_event_ids:
            started_at = perf_counter()
            try:
                publish_result = await self._outbox_publish_service.publish_event(
                    event_id
                )
            except Exception:  # noqa: BLE001 -- webhook success is already durable
                logger.info(
                    "ingestion_outbox_publish",
                    extra={
                        "delivery_id": str(result.delivery_id),
                        "event_id": str(event_id),
                        "attempted": True,
                        "succeeded": False,
                        "failed": True,
                        "failure_category": "PUBLISH_EXCEPTION",
                        "latency_ms": round((perf_counter() - started_at) * 1000, 3),
                    },
                )
                continue
            failed = publish_result.result is PublishResultCode.FAILED
            logger.info(
                "ingestion_outbox_publish",
                extra={
                    "delivery_id": str(result.delivery_id),
                    "event_id": str(event_id),
                    "attempted": True,
                    "succeeded": not failed,
                    "failed": failed,
                    "failure_category": publish_result.failure_category,
                    "latency_ms": round((perf_counter() - started_at) * 1000, 3),
                },
            )


def _title(event: CanonicalAlertEvent) -> str:
    summary = event.annotations.get("summary")
    alert_name = event.labels.get("alertname")
    if summary:
        return summary
    if isinstance(alert_name, str) and alert_name:
        return alert_name
    return f"Grafana alert {event.fingerprint}"


def _severity(event: CanonicalAlertEvent) -> str:
    return event.severity.canonical
