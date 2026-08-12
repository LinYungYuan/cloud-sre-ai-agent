from __future__ import annotations

import json
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class JobRepository(Protocol):
    async def create_rca_work(
        self,
        *,
        incident_id: UUID,
        run_status: str,
        available_at: datetime,
    ) -> UUID: ...


class SqlAlchemyJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_rca_work(
        self,
        *,
        incident_id: UUID,
        run_status: str,
        available_at: datetime,
    ) -> UUID:
        proposed_run_id = uuid4()
        run_id = await self._session.scalar(
            text(
                """
                INSERT INTO rca_runs (
                    id, incident_id, status, created_at, updated_at
                ) VALUES (
                    :id, :incident_id, :status, :available_at, :available_at
                )
                ON CONFLICT DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": proposed_run_id,
                "incident_id": incident_id,
                "status": run_status,
                "available_at": available_at,
            },
        )
        if run_id is None:
            run_id = await self._session.scalar(
                text(
                    """
                    SELECT id FROM rca_runs
                    WHERE incident_id = :incident_id
                      AND status IN ('WAITING_FOR_CLASSIFICATION', 'QUEUED', 'RUNNING')
                    FOR UPDATE
                    """
                ),
                {"incident_id": incident_id},
            )
        if run_id is None:
            raise RuntimeError("active RCA run could not be created")

        payload = json.dumps(
            {"incident_id": str(incident_id), "rca_run_id": str(run_id)},
            separators=(",", ":"),
        )
        await self._session.execute(
            text(
                """
                INSERT INTO worker_jobs (
                    rca_run_id, job_type, status, payload, available_at,
                    created_at, updated_at
                ) VALUES (
                    :run_id, 'RCA_RUN', 'QUEUED', CAST(:payload AS jsonb),
                    :available_at, :available_at, :available_at
                )
                """
            ),
            {"run_id": run_id, "payload": payload, "available_at": available_at},
        )
        await self._session.execute(
            text(
                """
                INSERT INTO outbox_events (
                    aggregate_type, aggregate_id, event_type, payload,
                    idempotency_key, status, available_at, created_at
                ) VALUES (
                    'INCIDENT', :incident_id, 'RCA_RUN_REQUESTED',
                    CAST(:payload AS jsonb), :idempotency_key, 'PENDING',
                    :available_at, :available_at
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                """
            ),
            {
                "incident_id": incident_id,
                "payload": payload,
                "idempotency_key": f"rca-run:{run_id}",
                "available_at": available_at,
            },
        )
        return run_id
