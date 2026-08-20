from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sre_agent.domain.alerts.cross_cloud import AlertValidationError
from sre_agent.integrations.grafana.normalizer import CanonicalAlertEvent


@dataclass(frozen=True, slots=True)
class SourceScope:
    team_id: UUID
    project_id: UUID
    environment_id: UUID


@dataclass(frozen=True, slots=True)
class StoredAlertEvent:
    id: UUID
    partition_timestamp: datetime


class AlertRepository(Protocol):
    async def get_source_scope(self, source_id: UUID) -> SourceScope: ...

    async def create_delivery(
        self,
        *,
        source_id: UUID,
        token_id: str,
        received_at: datetime,
        body_hash: str,
        raw_body: bytes,
        raw_payload: object,
        truncated_alerts: int,
        incomplete: bool,
    ) -> UUID: ...

    async def claim_dedup_key(
        self,
        *,
        source_id: UUID,
        dedup_key: str,
        delivery_id: UUID,
        delivery_partition_timestamp: datetime,
    ) -> bool: ...

    async def add_event(
        self,
        *,
        delivery_id: UUID,
        received_at: datetime,
        event: CanonicalAlertEvent,
        raw_payload: object,
        validation_status: str,
        validation_errors: Sequence[AlertValidationError],
    ) -> StoredAlertEvent: ...

    async def upsert_instance(
        self,
        *,
        event: CanonicalAlertEvent,
        stored_event: StoredAlertEvent,
        received_at: datetime,
    ) -> None: ...

    async def finish_delivery(
        self,
        *,
        delivery_id: UUID,
        partition_timestamp: datetime,
        status: str,
        processed_at: datetime,
    ) -> None: ...


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(nested) for key, nested in value.items()}
    if isinstance(value, tuple | list):
        return [_plain_json(nested) for nested in value]
    return value


class SqlAlchemyAlertRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_source_scope(self, source_id: UUID) -> SourceScope:
        row = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT project.team_id, source.project_id, source.environment_id
                    FROM grafana_sources AS source
                    JOIN projects AS project ON project.id = source.project_id
                    WHERE source.id = :source_id AND source.enabled
                    """
                    ),
                    {"source_id": source_id},
                )
            )
            .mappings()
            .one()
        )
        return SourceScope(
            team_id=row["team_id"],
            project_id=row["project_id"],
            environment_id=row["environment_id"],
        )

    async def create_delivery(
        self,
        *,
        source_id: UUID,
        token_id: str,
        received_at: datetime,
        body_hash: str,
        raw_body: bytes,
        raw_payload: object,
        truncated_alerts: int,
        incomplete: bool,
    ) -> UUID:
        delivery_id = uuid4()
        await self._session.execute(
            text(
                """
                INSERT INTO webhook_deliveries (
                    id, partition_timestamp, received_at, source_id, token_id,
                    body_hash, raw_body, raw_payload, truncated_alerts,
                    incomplete, status
                ) VALUES (
                    :id, :received_at, :received_at, :source_id, :token_id,
                    :body_hash, :raw_body, CAST(:raw_payload AS jsonb),
                    :truncated_alerts, :incomplete, 'RECEIVED'
                )
                """
            ),
            {
                "id": delivery_id,
                "received_at": received_at,
                "source_id": source_id,
                "token_id": token_id,
                "body_hash": body_hash,
                "raw_body": raw_body,
                "raw_payload": _json(raw_payload),
                "truncated_alerts": truncated_alerts,
                "incomplete": incomplete,
            },
        )
        return delivery_id

    async def claim_dedup_key(
        self,
        *,
        source_id: UUID,
        dedup_key: str,
        delivery_id: UUID,
        delivery_partition_timestamp: datetime,
    ) -> bool:
        claimed = await self._session.scalar(
            text(
                """
                INSERT INTO ingestion_dedup_keys (
                    source_id, dedup_key, delivery_id, delivery_partition_timestamp
                ) VALUES (
                    :source_id, :dedup_key, :delivery_id, :partition_timestamp
                )
                ON CONFLICT (source_id, dedup_key) DO NOTHING
                RETURNING id
                """
            ),
            {
                "source_id": source_id,
                "dedup_key": dedup_key,
                "delivery_id": delivery_id,
                "partition_timestamp": delivery_partition_timestamp,
            },
        )
        return claimed is not None

    async def add_event(
        self,
        *,
        delivery_id: UUID,
        received_at: datetime,
        event: CanonicalAlertEvent,
        raw_payload: object,
        validation_status: str,
        validation_errors: Sequence[AlertValidationError],
    ) -> StoredAlertEvent:
        event_id = uuid4()
        await self._session.execute(
            text(
                """
                INSERT INTO alert_events (
                    id, partition_timestamp, observed_at, source_id, delivery_id,
                    delivery_partition_timestamp, fingerprint, alert_state,
                    validation_status, validation_errors, starts_at, ends_at,
                    labels, annotations, raw_payload, provider, folder_code,
                    alert_name, severity_raw, severity_canonical, issue,
                    resource, normalization_status, normalization_rule_id,
                    normalization_rule_version, normalization_warnings
                ) VALUES (
                    :id, :received_at, :received_at, :source_id, :delivery_id,
                    :received_at, :fingerprint, :alert_state, :validation_status,
                    CAST(:validation_errors AS jsonb), :starts_at, :ends_at,
                    CAST(:labels AS jsonb), CAST(:annotations AS jsonb),
                    CAST(:raw_payload AS jsonb), :provider, :folder_code,
                    :alert_name, :severity_raw, :severity_canonical,
                    CAST(:issue AS jsonb), CAST(:resource AS jsonb),
                    :normalization_status, :normalization_rule_id,
                    :normalization_rule_version,
                    CAST(:normalization_warnings AS jsonb)
                )
                """
            ),
            {
                "id": event_id,
                "received_at": received_at,
                "source_id": event.source_id,
                "delivery_id": delivery_id,
                "fingerprint": event.fingerprint,
                "alert_state": event.status.value,
                "validation_status": validation_status,
                "validation_errors": _json(
                    [
                        {"field": error.field, "code": error.code}
                        for error in validation_errors
                    ]
                ),
                "starts_at": event.starts_at,
                "ends_at": event.ends_at,
                "labels": _json(dict(event.labels)),
                "annotations": _json(dict(event.annotations)),
                "raw_payload": _json(raw_payload),
                "provider": event.provider.value,
                "folder_code": event.folder_code,
                "alert_name": event.alert_name,
                "severity_raw": event.severity.raw,
                "severity_canonical": event.severity.canonical,
                "issue": _json(
                    {
                        "rawText": event.issue.raw_text,
                        "source": event.issue.source,
                        "contentType": event.issue.content_type,
                        "untrusted": event.issue.untrusted,
                    }
                ),
                "resource": (
                    None
                    if event.resource is None
                    else _json(
                        {
                            "provider": event.resource.provider.value,
                            "resourceType": event.resource.resource_type,
                            "scopeId": event.resource.scope_id,
                            "resourceId": event.resource.resource_id,
                            "resourceName": event.resource.resource_name,
                            "displayUnit": event.resource.display_unit,
                        }
                    )
                ),
                "normalization_status": event.normalization_status.value,
                "normalization_rule_id": event.normalization_rule_id,
                "normalization_rule_version": event.normalization_rule_version,
                "normalization_warnings": _json(event.normalization_warnings),
            },
        )
        return StoredAlertEvent(event_id, received_at)

    async def upsert_instance(
        self,
        *,
        event: CanonicalAlertEvent,
        stored_event: StoredAlertEvent,
        received_at: datetime,
    ) -> None:
        resolved_at = received_at if event.status.value == "RESOLVED" else None
        await self._session.execute(
            text(
                """
                INSERT INTO alert_instances (
                    source_id, fingerprint, latest_event_id,
                    latest_event_partition_timestamp, state, labels, annotations,
                    first_seen_at, last_seen_at, resolved_at
                ) VALUES (
                    :source_id, :fingerprint, :event_id, :partition_timestamp,
                    :state, CAST(:labels AS jsonb), CAST(:annotations AS jsonb),
                    :received_at, :received_at, :resolved_at
                )
                ON CONFLICT (source_id, fingerprint) DO UPDATE SET
                    latest_event_id = EXCLUDED.latest_event_id,
                    latest_event_partition_timestamp = EXCLUDED.latest_event_partition_timestamp,
                    state = EXCLUDED.state,
                    labels = EXCLUDED.labels,
                    annotations = EXCLUDED.annotations,
                    last_seen_at = EXCLUDED.last_seen_at,
                    resolved_at = EXCLUDED.resolved_at,
                    version = alert_instances.version + 1
                """
            ),
            {
                "source_id": event.source_id,
                "fingerprint": event.fingerprint,
                "event_id": stored_event.id,
                "partition_timestamp": stored_event.partition_timestamp,
                "state": event.status.value,
                "labels": _json(_plain_json(event.labels)),
                "annotations": _json(_plain_json(event.annotations)),
                "received_at": received_at,
                "resolved_at": resolved_at,
            },
        )

    async def finish_delivery(
        self,
        *,
        delivery_id: UUID,
        partition_timestamp: datetime,
        status: str,
        processed_at: datetime,
    ) -> None:
        await self._session.execute(
            text(
                """
                UPDATE webhook_deliveries
                SET status = :status, processed_at = :processed_at
                WHERE id = :delivery_id
                  AND partition_timestamp = :partition_timestamp
                """
            ),
            {
                "status": status,
                "processed_at": processed_at,
                "delivery_id": delivery_id,
                "partition_timestamp": partition_timestamp,
            },
        )
