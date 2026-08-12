from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sre_agent.domain.alerts.classification import (
    AlertScope,
    ClassificationResult,
)
from sre_agent.domain.alerts.fingerprint import hash_raw_body
from sre_agent.domain.alerts.models import AlertState, ClassificationStatus
from sre_agent.domain.common import require_aware_utc
from sre_agent.integrations.grafana.normalizer import (
    CanonicalAlertEvent,
    normalize_alerts,
)
from sre_agent.integrations.grafana.payloads import parse_grafana_body
from sre_agent.persistence.repositories.alerts import SourceScope
from sre_agent.persistence.repositories.incidents import IncidentScope
from sre_agent.persistence.unit_of_work import UnitOfWork


class Classifier(Protocol):
    def classify(
        self,
        labels: Mapping[str, str],
        rule_uid: str | None,
        folder: str | None,
    ) -> ClassificationResult: ...


@dataclass(frozen=True, slots=True)
class IngestionResult:
    delivery_id: UUID
    accepted_at: datetime
    incident_ids: tuple[UUID, ...]


class IngestGrafanaAlerts:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        classifier: Classifier,
        max_body_bytes: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._classifier = classifier
        self._max_body_bytes = max_body_bytes

    async def execute(
        self,
        source_id: UUID,
        token_id: str,
        raw_body: bytes,
        received_at: datetime,
    ) -> IngestionResult:
        accepted_at = require_aware_utc(received_at)
        webhook = parse_grafana_body(raw_body, self._max_body_bytes)
        canonical_events = normalize_alerts(source_id, webhook)
        raw_document = json.loads(raw_body)
        raw_alerts = raw_document["alerts"]
        if not isinstance(raw_alerts, list) or len(raw_alerts) != len(canonical_events):
            raise ValueError("Grafana raw alert list does not match normalized events")

        incident_ids: list[UUID] = []
        async with self._uow_factory() as uow:
            source_scope = await uow.alerts.lock_source_scope(source_id)
            body_hash = hash_raw_body(raw_body)
            delivery_id = await uow.alerts.create_delivery(
                source_id=source_id,
                token_id=token_id,
                received_at=accepted_at,
                body_hash=body_hash,
                raw_payload=raw_document,
            )

            new_event_count = 0
            for event, raw_alert in zip(canonical_events, raw_alerts, strict=True):
                claimed = await uow.alerts.claim_dedup_key(
                    source_id=source_id,
                    dedup_key=event.dedup_key,
                    delivery_id=delivery_id,
                    delivery_partition_timestamp=accepted_at,
                )
                if not claimed:
                    continue
                new_event_count += 1
                stored_event = await uow.alerts.add_event(
                    delivery_id=delivery_id,
                    received_at=accepted_at,
                    event=event,
                    raw_payload=raw_alert,
                )
                await uow.alerts.upsert_instance(
                    event=event,
                    stored_event=stored_event,
                    received_at=accepted_at,
                )

                classification = self._classify(event)
                incident_scope = _incident_scope(classification.scope, source_scope)
                incident_id: UUID | None
                if event.status is AlertState.FIRING:
                    candidate_id = await uow.incidents.lock_active_candidate(
                        incident_scope
                    )
                    is_new_incident = candidate_id is None
                    if is_new_incident:
                        reopened_from = await uow.incidents.latest_resolved_candidate(
                            incident_scope
                        )
                        incident_id = await uow.incidents.create(
                            scope=incident_scope,
                            title=_title(event),
                            severity=_severity(event),
                            opened_at=accepted_at,
                            reopened_from_incident_id=reopened_from,
                        )
                    else:
                        incident_id = candidate_id
                    await uow.incidents.link_alert(incident_id, stored_event)
                    if not is_new_incident:
                        await uow.incidents.set_alert_state(
                            incident_id, AlertState.FIRING.value, accepted_at
                        )
                    else:
                        run_status = (
                            "QUEUED"
                            if classification.status is ClassificationStatus.CLASSIFIED
                            else "WAITING_FOR_CLASSIFICATION"
                        )
                        await uow.jobs.create_rca_work(
                            incident_id=incident_id,
                            run_status=run_status,
                            available_at=accepted_at,
                        )
                else:
                    incident_id = await uow.incidents.lock_for_alert(
                        source_id, event.fingerprint
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
                partition_timestamp=accepted_at,
                status="PROCESSED" if new_event_count else "DUPLICATE",
                processed_at=accepted_at,
            )

        return IngestionResult(delivery_id, accepted_at, tuple(incident_ids))

    def _classify(self, event: CanonicalAlertEvent) -> ClassificationResult:
        return self._classifier.classify(
            event.labels,
            rule_uid=event.labels.get("grafana_rule_uid")
            or event.labels.get("rule_uid"),
            folder=event.labels.get("grafana_folder") or event.labels.get("folder"),
        )


def _incident_scope(
    classified: AlertScope,
    source: SourceScope,
) -> IncidentScope:
    return IncidentScope(
        team_id=classified.team_id or source.team_id,
        project_id=classified.project_id or source.project_id,
        environment_id=classified.environment_id or source.environment_id,
        service_id=classified.service_id,
    )


def _title(event: CanonicalAlertEvent) -> str:
    return (
        event.annotations.get("summary")
        or event.labels.get("alertname")
        or f"Grafana alert {event.fingerprint}"
    )


def _severity(event: CanonicalAlertEvent) -> str:
    severity = event.labels.get("severity", "").upper()
    if severity in {"SEV1", "SEV2", "SEV3", "SEV4"}:
        return severity
    return "SEV3"
