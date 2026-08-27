from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sre_agent.integrations.pubsub.messages import RcaJobMessage


@dataclass(frozen=True, slots=True)
class RcaWorkCreation:
    run_id: UUID
    outbox_event_id: UUID | None


class JobRepository(Protocol):
    async def create_rca_work(
        self,
        *,
        incident_id: UUID,
        run_status: str,
        available_at: datetime,
    ) -> RcaWorkCreation: ...


class SqlAlchemyJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_rca_work(
        self,
        *,
        incident_id: UUID,
        run_status: str,
        available_at: datetime,
    ) -> RcaWorkCreation:
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
            existing_run_id = await self._session.scalar(
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
            if existing_run_id is None:
                raise RuntimeError("active RCA run could not be created")
            return RcaWorkCreation(existing_run_id, None)

        proposed_job_id = uuid4()
        worker_job_id = await self._session.scalar(
            text(
                """
                INSERT INTO worker_jobs (
                    id, rca_run_id, job_type, status, payload, available_at,
                    created_at, updated_at
                ) VALUES (
                    :job_id, :run_id, 'RCA_ANALYSIS', 'QUEUED', '{}'::jsonb,
                    :available_at, :available_at, :available_at
                )
                ON CONFLICT (rca_run_id, job_type) DO NOTHING
                RETURNING id
                """
            ),
            {
                "job_id": proposed_job_id,
                "run_id": run_id,
                "available_at": available_at,
            },
        )
        if worker_job_id is None:
            worker_job_id = await self._session.scalar(
                text(
                    """SELECT id FROM worker_jobs
                       WHERE rca_run_id = :run_id AND job_type = 'RCA_ANALYSIS'"""
                ),
                {"run_id": run_id},
            )
        if worker_job_id is None:
            raise RuntimeError("RCA worker job could not be created")

        message = RcaJobMessage.from_mapping(
            {
                "schemaVersion": 1,
                "workerJobId": worker_job_id,
                "rcaRunId": run_id,
                "incidentId": incident_id,
                "attempt": 1,
            }
        )
        payload = message.to_bytes().decode()
        await self._session.execute(
            text(
                """UPDATE worker_jobs SET payload = CAST(:payload AS jsonb)
                   WHERE id = :job_id"""
            ),
            {"job_id": worker_job_id, "payload": payload},
        )
        proposed_event_id = uuid4()
        outbox_event_id = await self._session.scalar(
            text(
                """
                INSERT INTO outbox_events (
                    id, aggregate_type, aggregate_id, event_type, payload,
                    idempotency_key, status, available_at, created_at
                ) VALUES (
                    :event_id, 'INCIDENT', :incident_id, 'RCA_RUN_REQUESTED',
                    CAST(:payload AS jsonb), :idempotency_key, 'PENDING',
                    :available_at, :available_at
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """
            ),
            {
                "event_id": proposed_event_id,
                "incident_id": incident_id,
                "payload": payload,
                "idempotency_key": f"rca-run:{run_id}",
                "available_at": available_at,
            },
        )
        if outbox_event_id is None:
            existing_event_id = await self._session.scalar(
                text(
                    """
                    SELECT id FROM outbox_events
                    WHERE idempotency_key = :idempotency_key
                    """
                ),
                {"idempotency_key": f"rca-run:{run_id}"},
            )
            if existing_event_id is None:
                raise RuntimeError("RCA outbox event could not be created")
        return RcaWorkCreation(run_id, outbox_event_id)
