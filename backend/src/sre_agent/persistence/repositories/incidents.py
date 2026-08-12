from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sre_agent.persistence.repositories.alerts import StoredAlertEvent


@dataclass(frozen=True, slots=True)
class IncidentScope:
    team_id: UUID
    project_id: UUID
    environment_id: UUID
    service_id: UUID | None


class IncidentRepository(Protocol):
    async def lock_active_candidate(self, scope: IncidentScope) -> UUID | None: ...

    async def latest_resolved_candidate(self, scope: IncidentScope) -> UUID | None: ...

    async def create(
        self,
        *,
        scope: IncidentScope,
        title: str,
        severity: str,
        opened_at: datetime,
        reopened_from_incident_id: UUID | None,
    ) -> UUID: ...

    async def lock_for_alert(
        self, source_id: UUID, fingerprint: str
    ) -> UUID | None: ...

    async def link_alert(
        self, incident_id: UUID, stored_event: StoredAlertEvent
    ) -> None: ...

    async def set_alert_state(
        self, incident_id: UUID, alert_state: str, updated_at: datetime
    ) -> None: ...


class SqlAlchemyIncidentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_active_candidate(self, scope: IncidentScope) -> UUID | None:
        return await self._session.scalar(
            text(
                """
                SELECT id
                FROM incidents
                WHERE status IN ('OPEN', 'INVESTIGATING')
                  AND team_id = :team_id
                  AND project_id = :project_id
                  AND environment_id = :environment_id
                  AND service_id IS NOT DISTINCT FROM :service_id
                ORDER BY opened_at DESC, id
                LIMIT 1
                FOR UPDATE
                """
            ),
            {
                "team_id": scope.team_id,
                "project_id": scope.project_id,
                "environment_id": scope.environment_id,
                "service_id": scope.service_id,
            },
        )

    async def latest_resolved_candidate(self, scope: IncidentScope) -> UUID | None:
        return await self._session.scalar(
            text(
                """
                SELECT id
                FROM incidents
                WHERE status = 'RESOLVED'
                  AND team_id = :team_id
                  AND project_id = :project_id
                  AND environment_id = :environment_id
                  AND service_id IS NOT DISTINCT FROM :service_id
                ORDER BY resolved_at DESC NULLS LAST, opened_at DESC, id
                LIMIT 1
                """
            ),
            {
                "team_id": scope.team_id,
                "project_id": scope.project_id,
                "environment_id": scope.environment_id,
                "service_id": scope.service_id,
            },
        )

    async def create(
        self,
        *,
        scope: IncidentScope,
        title: str,
        severity: str,
        opened_at: datetime,
        reopened_from_incident_id: UUID | None,
    ) -> UUID:
        incident_id = uuid4()
        await self._session.execute(
            text(
                """
                INSERT INTO incidents (
                    id, title, severity, status, alert_state, team_id, project_id,
                    environment_id, service_id, opened_at, reopened_from_incident_id,
                    created_at, updated_at
                ) VALUES (
                    :id, :title, :severity, 'OPEN', 'FIRING', :team_id, :project_id,
                    :environment_id, :service_id, :opened_at, :reopened_from,
                    :opened_at, :opened_at
                )
                """
            ),
            {
                "id": incident_id,
                "title": title,
                "severity": severity,
                "team_id": scope.team_id,
                "project_id": scope.project_id,
                "environment_id": scope.environment_id,
                "service_id": scope.service_id,
                "opened_at": opened_at,
                "reopened_from": reopened_from_incident_id,
            },
        )
        return incident_id

    async def lock_for_alert(self, source_id: UUID, fingerprint: str) -> UUID | None:
        return await self._session.scalar(
            text(
                """
                SELECT incident.id
                FROM incidents AS incident
                JOIN incident_alerts AS link ON link.incident_id = incident.id
                JOIN alert_events AS event
                  ON event.id = link.alert_event_id
                 AND event.partition_timestamp = link.alert_event_partition_timestamp
                WHERE event.source_id = :source_id
                  AND event.fingerprint = :fingerprint
                ORDER BY incident.opened_at DESC, incident.id
                LIMIT 1
                FOR UPDATE OF incident
                """
            ),
            {"source_id": source_id, "fingerprint": fingerprint},
        )

    async def link_alert(
        self, incident_id: UUID, stored_event: StoredAlertEvent
    ) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO incident_alerts (
                    incident_id, alert_event_id, alert_event_partition_timestamp
                ) VALUES (:incident_id, :event_id, :partition_timestamp)
                ON CONFLICT (
                    incident_id, alert_event_id, alert_event_partition_timestamp
                ) DO NOTHING
                """
            ),
            {
                "incident_id": incident_id,
                "event_id": stored_event.id,
                "partition_timestamp": stored_event.partition_timestamp,
            },
        )

    async def set_alert_state(
        self, incident_id: UUID, alert_state: str, updated_at: datetime
    ) -> None:
        await self._session.execute(
            text(
                """
                UPDATE incidents
                SET alert_state = :alert_state,
                    updated_at = :updated_at,
                    version = version + 1
                WHERE id = :incident_id
                """
            ),
            {
                "alert_state": alert_state,
                "updated_at": updated_at,
                "incident_id": incident_id,
            },
        )
