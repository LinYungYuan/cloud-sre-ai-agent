import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sre_rca_worker.application.rca.job_lifecycle import (
    JobDisposition,
    RcaJobHandler,
    RcaProcessingResult,
)
from sre_rca_worker.integrations.pubsub.messages import RcaJobMessage

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
)


async def _seed(session_factory) -> RcaJobMessage:
    ids = [uuid4() for _ in range(6)]
    team, project, environment, incident, run, job = ids
    async with session_factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO teams(id,name) VALUES (:id,:name)"),
            {"id": team, "name": f"team-{team}"},
        )
        await session.execute(
            text("INSERT INTO projects(id,team_id,name) VALUES (:id,:team,:name)"),
            {"id": project, "team": team, "name": f"project-{project}"},
        )
        await session.execute(
            text(
                "INSERT INTO environments(id,project_id,name) VALUES (:id,:project,:name)"
            ),
            {"id": environment, "project": project, "name": f"env-{environment}"},
        )
        await session.execute(
            text("""INSERT INTO incidents(id,identity_key,title,severity,status,alert_state,team_id,project_id,environment_id,opened_at)
          VALUES (:id,:identity,'test','SEV3','OPEN','FIRING',:team,:project,:environment,now())"""),
            {
                "id": incident,
                "identity": f"identity-{incident}",
                "team": team,
                "project": project,
                "environment": environment,
            },
        )
        await session.execute(
            text(
                "INSERT INTO rca_runs(id,incident_id,status) VALUES (:id,:incident,'QUEUED')"
            ),
            {"id": run, "incident": incident},
        )
        await session.execute(
            text(
                "INSERT INTO worker_jobs(id,rca_run_id,job_type,status,payload) VALUES (:id,:run,'RCA_ANALYSIS','QUEUED','{}')"
            ),
            {"id": job, "run": run},
        )
    return RcaJobMessage(
        schemaVersion=1, workerJobId=job, rcaRunId=run, incidentId=incident, attempt=1
    )


@pytest.mark.asyncio
async def test_success_commits_terminal_state_before_ack_disposition() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    message = await _seed(sessions)
    calls: list[UUID] = []

    async def process(claim):
        calls.append(claim.worker_job_id)
        return RcaProcessingResult(status="COMPLETE")

    handler = RcaJobHandler(sessions, process, worker_id="worker-a")
    assert await handler.handle(message) is JobDisposition.ACK
    assert await handler.handle(message) is JobDisposition.ACK
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status,attempt_count,lease_owner FROM worker_jobs WHERE id=:id"
                ),
                {"id": message.worker_job_id},
            )
        ).one()
        assert row == ("SUCCEEDED", 1, None)
    assert calls == [message.worker_job_id]
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_consumers_only_execute_once() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    message = await _seed(sessions)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def process(claim):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return RcaProcessingResult(status="PARTIAL")

    first = asyncio.create_task(
        RcaJobHandler(sessions, process, worker_id="a").handle(message)
    )
    await entered.wait()
    second = await RcaJobHandler(sessions, process, worker_id="b").handle(message)
    release.set()
    assert await first is JobDisposition.ACK
    assert second is JobDisposition.NACK
    assert calls == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_identifier_mismatch_is_acked_without_running_job() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    message = await _seed(sessions)

    async def process(claim):
        raise AssertionError("must not process")

    invalid = message.model_copy(update={"incident_id": uuid4()})
    assert (
        await RcaJobHandler(sessions, process, worker_id="a").handle(invalid)
        is JobDisposition.ACK
    )
    async with sessions() as session:
        assert (
            await session.scalar(
                text("SELECT status FROM worker_jobs WHERE id=:id"),
                {"id": message.worker_job_id},
            )
            == "QUEUED"
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_outer_cancellation_propagates_and_lease_is_recoverable() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    message = await _seed(sessions)
    entered = asyncio.Event()

    async def process(claim):
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        RcaJobHandler(sessions, process, worker_id="cancelled").handle(message)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with sessions() as session:
        row = (
            await session.execute(
                text("SELECT status,lease_owner FROM worker_jobs WHERE id=:id"),
                {"id": message.worker_job_id},
            )
        ).one()
        assert row == ("RUNNING", "cancelled")
    await engine.dispose()


@pytest.mark.asyncio
async def test_long_processing_renews_lease_before_terminal_commit() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    message = await _seed(sessions)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def process(claim):
        entered.set()
        await release.wait()
        return RcaProcessingResult(status="COMPLETE")

    task = asyncio.create_task(
        RcaJobHandler(
            sessions,
            process,
            worker_id="renewing",
            lease_renewal_seconds=0.02,
        ).handle(message)
    )
    await entered.wait()
    async with sessions() as session:
        first_expiry = await session.scalar(
            text("SELECT lease_expires_at FROM worker_jobs WHERE id=:id"),
            {"id": message.worker_job_id},
        )
    await asyncio.sleep(0.05)
    async with sessions() as session:
        renewed_expiry = await session.scalar(
            text("SELECT lease_expires_at FROM worker_jobs WHERE id=:id"),
            {"id": message.worker_job_id},
        )
    assert renewed_expiry > first_expiry
    release.set()
    assert await task is JobDisposition.ACK
    await engine.dispose()


@pytest.mark.asyncio
async def test_transient_failure_nacks_twice_then_terminally_acks_third_attempt() -> (
    None
):
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    message = await _seed(sessions)
    calls = 0

    async def process(claim):
        nonlocal calls
        calls += 1
        raise ConnectionError("temporary MCP transport details")

    handler = RcaJobHandler(sessions, process, worker_id="retrying")
    assert await handler.handle(message) is JobDisposition.NACK
    for expected in (JobDisposition.NACK, JobDisposition.ACK):
        async with sessions() as session, session.begin():
            await session.execute(
                text("UPDATE worker_jobs SET available_at=now() WHERE id=:id"),
                {"id": message.worker_job_id},
            )
        assert await handler.handle(message) is expected

    async with sessions() as session:
        job = (
            await session.execute(
                text(
                    "SELECT status,attempt_count,lease_owner FROM worker_jobs WHERE id=:id"
                ),
                {"id": message.worker_job_id},
            )
        ).one()
        run_status = await session.scalar(
            text("SELECT status FROM rca_runs WHERE id=:id"),
            {"id": message.rca_run_id},
        )
        attempts = await session.scalar(
            text("SELECT count(*) FROM worker_attempts WHERE worker_job_id=:id"),
            {"id": message.worker_job_id},
        )
    assert job == ("FAILED", 3, None)
    assert run_status == "FAILED"
    assert attempts == 3
    assert calls == 3
    await engine.dispose()
