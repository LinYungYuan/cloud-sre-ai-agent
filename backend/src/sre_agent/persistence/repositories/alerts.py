from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
    async def lock_source_scope(self, source_id: UUID) -> SourceScope: ...

    async def create_delivery(
        self,
        *,
        source_id: UUID,
        token_id: str,
        received_at: datetime,
        body_hash: str,
        raw_payload: object,
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

    async def lock_source_scope(self, source_id: UUID) -> SourceScope:
        row = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT project.team_id, source.project_id, source.environment_id
                    FROM grafana_sources AS source
                    JOIN projects AS project ON project.id = source.project_id
                    WHERE source.id = :source_id AND source.enabled
                    FOR UPDATE OF source
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
        raw_payload: object,
    ) -> UUID:
        delivery_id = uuid4()
        await self._session.execute(
            text(
                """
                INSERT INTO webhook_deliveries (
                    id, partition_timestamp, received_at, source_id, token_id,
                    body_hash, raw_payload, status
                ) VALUES (
                    :id, :received_at, :received_at, :source_id, :token_id,
                    :body_hash, CAST(:raw_payload AS jsonb), 'RECEIVED'
                )
                """
            ),
            {
                "id": delivery_id,
                "received_at": received_at,
                "source_id": source_id,
                "token_id": token_id,
                "body_hash": body_hash,
                "raw_payload": _json(raw_payload),
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
    ) -> StoredAlertEvent:
        event_id = uuid4()
        await self._session.execute(
            text(
                """
                INSERT INTO alert_events (
                    id, partition_timestamp, observed_at, source_id, delivery_id,
                    delivery_partition_timestamp, fingerprint, alert_state,
                    starts_at, ends_at, labels, annotations, raw_payload
                ) VALUES (
                    :id, :received_at, :received_at, :source_id, :delivery_id,
                    :received_at, :fingerprint, :alert_state, :starts_at, :ends_at,
                    CAST(:labels AS jsonb), CAST(:annotations AS jsonb),
                    CAST(:raw_payload AS jsonb)
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
                "starts_at": event.starts_at,
                "ends_at": event.ends_at,
                "labels": _json(dict(event.labels)),
                "annotations": _json(dict(event.annotations)),
                "raw_payload": _json(raw_payload),
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
