from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sre_rca_worker.integrations.pubsub.messages import RcaJobMessage


class JobDisposition(StrEnum):
    ACK = "ACK"
    NACK = "NACK"


class RcaProcessingResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["COMPLETE", "PARTIAL", "FAILED"]


@dataclass(frozen=True, slots=True)
class RcaJobClaim:
    worker_job_id: UUID
    rca_run_id: UUID
    incident_id: UUID
    attempt_number: int
    deadline_at: datetime
    lease_owner: str


Processor = Callable[[RcaJobClaim], Awaitable[RcaProcessingResult]]


class RcaJobHandler:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        processor: Processor,
        *,
        worker_id: str,
        lease_renewal_seconds: float = 20,
    ) -> None:
        self._sessions = sessions
        self._processor = processor
        self._worker_id = worker_id
        self._lease_renewal_seconds = lease_renewal_seconds

    async def handle(self, message: RcaJobMessage) -> JobDisposition:
        claim = await self._claim(message)
        if claim is None:
            return await self._unclaimed_disposition(message)
        try:
            result = await self._execute_with_lease(claim)
        except LeaseLostError:
            return JobDisposition.NACK
        except Exception:  # noqa: BLE001 - durable boundary stores only safe codes
            return await self._settle_failure(claim)
        return await self._settle_success(claim, result)

    async def _execute_with_lease(self, claim: RcaJobClaim) -> RcaProcessingResult:
        async def invoke() -> RcaProcessingResult:
            return await self._processor(claim)

        processor = asyncio.create_task(invoke())
        renewal = asyncio.create_task(self._renew_lease(claim, processor))
        try:
            done, _ = await asyncio.wait(
                {processor, renewal}, return_when=asyncio.FIRST_COMPLETED
            )
            if processor in done:
                return await processor
            if renewal in done:
                await renewal
                raise LeaseLostError
            raise RuntimeError("lease execution reached an impossible state")
        finally:
            for task in (processor, renewal):
                if not task.done():
                    task.cancel()
            await asyncio.gather(processor, renewal, return_exceptions=True)

    async def _renew_lease(
        self, claim: RcaJobClaim, processor: asyncio.Task[RcaProcessingResult]
    ) -> None:
        while not processor.done():
            await asyncio.sleep(self._lease_renewal_seconds)
            if processor.done():
                return
            async with self._sessions() as session, session.begin():
                renewed = await session.scalar(
                    text(
                        """UPDATE worker_jobs
                           SET lease_expires_at=now()+interval '60 seconds', updated_at=now()
                           WHERE id=:id AND status='RUNNING' AND lease_owner=:owner
                           RETURNING id"""
                    ),
                    {"id": claim.worker_job_id, "owner": claim.lease_owner},
                )
            if renewed is None:
                raise LeaseLostError

    async def _claim(self, message: RcaJobMessage) -> RcaJobClaim | None:
        async with self._sessions() as session, session.begin():
            row = (
                (
                    await session.execute(
                        text(
                            """UPDATE worker_jobs AS job
                           SET status='RUNNING', lease_owner=:owner,
                               lease_expires_at=now() + interval '60 seconds',
                               attempt_count=job.attempt_count + 1,
                               started_at=COALESCE(job.started_at, now()), updated_at=now()
                           FROM rca_runs AS run
                           WHERE job.id=:job_id AND job.rca_run_id=:run_id
                             AND run.id=job.rca_run_id AND run.incident_id=:incident_id
                             AND job.available_at <= now() AND job.attempt_count < 3
                             AND job.created_at + interval '5 minutes' > now()
                             AND (job.status='QUEUED' OR
                                  (job.status='RUNNING' AND job.lease_expires_at < now()))
                           RETURNING job.id, job.rca_run_id, run.incident_id,
                                     job.attempt_count,
                                     job.created_at + interval '5 minutes' AS deadline_at"""
                        ),
                        {
                            "owner": self._worker_id,
                            "job_id": message.worker_job_id,
                            "run_id": message.rca_run_id,
                            "incident_id": message.incident_id,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            await session.execute(
                text(
                    "UPDATE rca_runs SET status='RUNNING', started_at=COALESCE(started_at,now()), updated_at=now() WHERE id=:id"
                ),
                {"id": message.rca_run_id},
            )
            await session.execute(
                text(
                    """INSERT INTO worker_attempts(worker_job_id,attempt_number)
                       VALUES (:job_id,:attempt)"""
                ),
                {"job_id": message.worker_job_id, "attempt": row["attempt_count"]},
            )
            return RcaJobClaim(
                worker_job_id=row["id"],
                rca_run_id=row["rca_run_id"],
                incident_id=row["incident_id"],
                attempt_number=row["attempt_count"],
                deadline_at=row["deadline_at"],
                lease_owner=self._worker_id,
            )

    async def _unclaimed_disposition(self, message: RcaJobMessage) -> JobDisposition:
        async with self._sessions() as session:
            row = (
                (
                    await session.execute(
                        text(
                            """SELECT job.status, job.attempt_count, run.id AS run_id,
                                  run.incident_id, job.created_at + interval '5 minutes' <= now() AS expired
                           FROM worker_jobs job JOIN rca_runs run ON run.id=job.rca_run_id
                           WHERE job.id=:id"""
                        ),
                        {"id": message.worker_job_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return JobDisposition.ACK
        if (
            row["run_id"] != message.rca_run_id
            or row["incident_id"] != message.incident_id
        ):
            return JobDisposition.ACK
        if row["status"] in {"SUCCEEDED", "FAILED"}:
            return JobDisposition.ACK
        if row["expired"] or row["attempt_count"] >= 3:
            await self._mark_unclaimed_terminal(
                message,
                "DEADLINE_EXCEEDED" if row["expired"] else "INTERNAL_ERROR",
            )
            return JobDisposition.ACK
        return JobDisposition.NACK

    async def _mark_unclaimed_terminal(
        self, message: RcaJobMessage, failure_code: str
    ) -> None:
        async with self._sessions() as session, session.begin():
            updated = await session.scalar(
                text(
                    """UPDATE worker_jobs SET status='FAILED', completed_at=now(),
                              updated_at=now(), lease_owner=NULL, lease_expires_at=NULL
                       WHERE id=:job AND rca_run_id=:run AND status NOT IN ('SUCCEEDED','FAILED')
                       RETURNING id"""
                ),
                {"job": message.worker_job_id, "run": message.rca_run_id},
            )
            if updated is not None:
                await session.execute(
                    text(
                        """UPDATE rca_runs SET status='FAILED', failure_code=:failure,
                                  completed_at=now(), updated_at=now()
                           WHERE id=:run AND incident_id=:incident"""
                    ),
                    {
                        "failure": failure_code,
                        "run": message.rca_run_id,
                        "incident": message.incident_id,
                    },
                )

    async def _settle_success(
        self, claim: RcaJobClaim, result: RcaProcessingResult
    ) -> JobDisposition:
        job_status = "FAILED" if result.status == "FAILED" else "SUCCEEDED"
        run_status = {
            "COMPLETE": "SUCCEEDED",
            "PARTIAL": "PARTIAL",
            "FAILED": "FAILED",
        }[result.status]
        failure_code = "INTERNAL_ERROR" if result.status == "FAILED" else None
        async with self._sessions() as session, session.begin():
            updated = await session.scalar(
                text(
                    """UPDATE worker_jobs SET status=:status, completed_at=now(),
                              updated_at=now(), lease_owner=NULL, lease_expires_at=NULL
                       WHERE id=:id AND status='RUNNING' AND lease_owner=:owner
                       RETURNING id"""
                ),
                {
                    "status": job_status,
                    "id": claim.worker_job_id,
                    "owner": claim.lease_owner,
                },
            )
            if updated is None:
                return JobDisposition.NACK
            await session.execute(
                text(
                    "UPDATE rca_runs SET status=:status, completed_at=now(), updated_at=now(), failure_code=:failure WHERE id=:id"
                ),
                {"status": run_status, "failure": failure_code, "id": claim.rca_run_id},
            )
            await session.execute(
                text(
                    """UPDATE worker_attempts SET completed_at=now(), failure_code=:failure
                       WHERE worker_job_id=:id AND attempt_number=:attempt"""
                ),
                {
                    "failure": failure_code,
                    "id": claim.worker_job_id,
                    "attempt": claim.attempt_number,
                },
            )
        return JobDisposition.ACK

    async def _settle_failure(self, claim: RcaJobClaim) -> JobDisposition:
        terminal = claim.attempt_number >= 3
        async with self._sessions() as session, session.begin():
            updated = await session.scalar(
                text(
                    """UPDATE worker_jobs
                       SET status=:status, available_at=CASE WHEN :terminal THEN available_at ELSE now()+interval '30 seconds' END,
                           completed_at=CASE WHEN :terminal THEN now() ELSE NULL END,
                           updated_at=now(), lease_owner=NULL, lease_expires_at=NULL
                       WHERE id=:id AND status='RUNNING' AND lease_owner=:owner
                       RETURNING id"""
                ),
                {
                    "status": "FAILED" if terminal else "QUEUED",
                    "terminal": terminal,
                    "id": claim.worker_job_id,
                    "owner": claim.lease_owner,
                },
            )
            if updated is None:
                return JobDisposition.NACK
            await session.execute(
                text(
                    """UPDATE worker_attempts SET completed_at=now(), failure_code='MCP_TRANSPORT'
                       WHERE worker_job_id=:id AND attempt_number=:attempt"""
                ),
                {"id": claim.worker_job_id, "attempt": claim.attempt_number},
            )
            await session.execute(
                text(
                    "UPDATE rca_runs SET status=:status, failure_code=:failure, updated_at=now() WHERE id=:id"
                ),
                {
                    "status": "FAILED" if terminal else "QUEUED",
                    "failure": "MCP_TRANSPORT" if terminal else None,
                    "id": claim.rca_run_id,
                },
            )
        return JobDisposition.ACK if terminal else JobDisposition.NACK


class LeaseLostError(RuntimeError):
    pass
