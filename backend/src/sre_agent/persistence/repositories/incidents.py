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
    team_id: UUID | None = None
    project_id: UUID | None = None
    environment_id: UUID | None = None
    service_id: UUID | None = None

    @property
    def is_empty(self) -> bool:
        return all(
            value is None
            for value in (
                self.team_id,
                self.project_id,
                self.environment_id,
                self.service_id,
            )
        )


@dataclass(frozen=True, slots=True)
class IncidentSelection:
    id: UUID
    created: bool


class IncidentRepository(Protocol):
    async def get_or_create_active(
        self,
        *,
        identity_key: str,
        identity_version: int,
        provider: str,
        folder_code: str | None,
        alert_name: str | None,
        scope: IncidentScope,
        title: str,
        severity: str,
        opened_at: datetime,
        reopened_from_incident_id: UUID | None,
    ) -> IncidentSelection: ...

    async def latest_resolved(
        self, identity_key: str, identity_version: int
    ) -> UUID | None: ...

    async def lock_latest(
        self, identity_key: str, identity_version: int
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

    async def get_or_create_active(
        self,
        *,
        identity_key: str,
        identity_version: int,
        provider: str,
        folder_code: str | None,
        alert_name: str | None,
        scope: IncidentScope,
        title: str,
        severity: str,
        opened_at: datetime,
        reopened_from_incident_id: UUID | None,
    ) -> IncidentSelection:
        proposed_id = uuid4()
        created_id = await self._session.scalar(
            text(
                """
                INSERT INTO incidents (
                    id, identity_key, identity_version, provider, folder_code,
                    alert_name, title, severity, status, alert_state,
                    team_id, project_id, environment_id, service_id, opened_at,
                    reopened_from_incident_id, created_at, updated_at
                ) VALUES (
                    :id, :identity_key, :identity_version, :provider,
                    :folder_code, :alert_name, :title, :severity, 'OPEN', 'FIRING',
                    :team_id, :project_id, :environment_id, :service_id, :opened_at,
                    :reopened_from, :opened_at, :opened_at
                )
                ON CONFLICT (identity_version, identity_key)
                    WHERE status IN ('OPEN', 'INVESTIGATING')
                    DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": proposed_id,
                "identity_key": identity_key,
                "identity_version": identity_version,
                "provider": provider,
                "folder_code": folder_code,
                "alert_name": alert_name,
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
        if created_id is not None:
            return IncidentSelection(id=created_id, created=True)

        active_id = await self._session.scalar(
            text(
                """
                SELECT id
                FROM incidents
                WHERE identity_key = :identity_key
                  AND identity_version = :identity_version
                  AND status IN ('OPEN', 'INVESTIGATING')
                ORDER BY opened_at DESC, id
                LIMIT 1
                FOR UPDATE
                """
            ),
            {"identity_key": identity_key, "identity_version": identity_version},
        )
        if active_id is None:
            raise RuntimeError("active Incident could not be selected")
        return IncidentSelection(id=active_id, created=False)

    async def latest_resolved(
        self, identity_key: str, identity_version: int
    ) -> UUID | None:
        return await self._session.scalar(
            text(
                """
                SELECT id
                FROM incidents
                WHERE identity_key = :identity_key
                  AND identity_version = :identity_version
                  AND status = 'RESOLVED'
                ORDER BY resolved_at DESC NULLS LAST, opened_at DESC, id
                LIMIT 1
                """
            ),
            {"identity_key": identity_key, "identity_version": identity_version},
        )

    async def lock_latest(
        self, identity_key: str, identity_version: int
    ) -> UUID | None:
        return await self._session.scalar(
            text(
                """
                SELECT id
                FROM incidents
                WHERE identity_key = :identity_key
                  AND identity_version = :identity_version
                ORDER BY opened_at DESC, id
                LIMIT 1
                FOR UPDATE
                """
            ),
            {"identity_key": identity_key, "identity_version": identity_version},
        )

    async def link_alert(
        self, incident_id: UUID, stored_event: StoredAlertEvent
    ) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO incident_alerts (
                    incident_id, alert_event_id
                ) VALUES (:incident_id, :event_id)
                ON CONFLICT (incident_id, alert_event_id) DO NOTHING
                """
            ),
            {
                "incident_id": incident_id,
                "event_id": stored_event.id,
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
