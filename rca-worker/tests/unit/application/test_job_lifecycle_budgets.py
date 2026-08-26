from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Self, cast
from uuid import uuid4

import pytest

from sre_rca_worker.application.rca.job_lifecycle import (
    DeadlineExceededError,
    JobDisposition,
    LifecycleFailureCode,
    RcaJobClaim,
    RcaJobHandler,
    RcaProcessingResult,
)
from sre_rca_worker.integrations.pubsub.messages import RcaJobMessage


class _Result:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._row = row

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._row


class _Session:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.executions: list[tuple[str, dict[str, Any] | None]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Session:
        return self

    async def execute(
        self, statement: object, params: dict[str, Any] | None = None
    ) -> _Result:
        self.executions.append((str(statement), params))
        row, self.row = self.row, None
        return _Result(row)

    async def scalar(
        self, statement: object, params: dict[str, Any] | None = None
    ) -> object:
        self.executions.append((str(statement), params))
        return uuid4()


class _Sessions:
    def __init__(self, *sessions: _Session) -> None:
        self.sessions = list(sessions)

    def __call__(self) -> _Session:
        if not self.sessions:
            raise AssertionError("unexpected session allocation")
        return self.sessions.pop(0)


def _message() -> RcaJobMessage:
    return RcaJobMessage(
        schemaVersion=1,
        workerJobId=uuid4(),
        rcaRunId=uuid4(),
        incidentId=uuid4(),
        attempt=1,
    )


def test_handler_validates_a_deadline_between_one_and_the_five_minute_cap() -> None:
    message = _message()

    handler = RcaJobHandler(
        cast(Any, _Sessions()),
        lambda _claim: _completed(),
        worker_id="worker",
        deadline_seconds=45,
    )
    assert handler._deadline_seconds == 45

    for invalid in (0, 301, True, 45.0, "45"):
        with pytest.raises(ValueError, match="deadline_seconds"):
            RcaJobHandler(
                cast(Any, _Sessions()),
                lambda _claim: _completed(),
                worker_id="worker",
                deadline_seconds=cast(Any, invalid),
            )

    del message


@pytest.mark.asyncio
async def test_claim_binds_lower_deadline_and_returns_the_configured_expiry() -> None:
    message = _message()
    now = datetime.now(UTC)
    returned_deadline = now + timedelta(seconds=45)
    session = _Session(
        {
            "id": message.worker_job_id,
            "rca_run_id": message.rca_run_id,
            "incident_id": message.incident_id,
            "attempt_count": 1,
            "deadline_at": returned_deadline,
        }
    )
    handler = RcaJobHandler(
        cast(Any, _Sessions(session)),
        lambda _claim: _completed(),
        worker_id="worker",
        deadline_seconds=45,
    )

    claim = await handler._claim(message)

    assert claim is not None
    assert claim.deadline_at == returned_deadline
    claim_sql, claim_params = session.executions[0]
    assert "LEAST(job.created_at + interval '5 minutes'" in claim_sql
    assert "make_interval(secs => :configured)" in claim_sql
    assert claim_params == {
        "owner": "worker",
        "job_id": message.worker_job_id,
        "run_id": message.rca_run_id,
        "incident_id": message.incident_id,
        "configured": 45,
    }


@pytest.mark.asyncio
async def test_unclaimed_expiry_uses_the_same_lower_configured_deadline() -> None:
    message = _message()
    session = _Session(
        {
            "status": "QUEUED",
            "attempt_count": 0,
            "run_id": message.rca_run_id,
            "incident_id": message.incident_id,
            "expired": True,
        }
    )
    terminal = _Session()
    handler = RcaJobHandler(
        cast(Any, _Sessions(session, terminal)),
        lambda _claim: _completed(),
        worker_id="worker",
        deadline_seconds=45,
    )

    assert await handler._unclaimed_disposition(message) is JobDisposition.ACK

    select_sql, select_params = session.executions[0]
    assert "LEAST(job.created_at + interval '5 minutes'" in select_sql
    assert "make_interval(secs => :configured)" in select_sql
    assert select_params == {"id": message.worker_job_id, "configured": 45}


@pytest.mark.asyncio
async def test_hung_processor_is_cancelled_at_claim_deadline_and_settled_terminally() -> (
    None
):
    message = _message()
    now = datetime.now(UTC)
    claim = RcaJobClaim(
        worker_job_id=message.worker_job_id,
        rca_run_id=message.rca_run_id,
        incident_id=message.incident_id,
        attempt_number=1,
        deadline_at=now + timedelta(milliseconds=40),
        lease_owner="worker",
    )
    started = False
    cancelled = False
    settled: list[str] = []
    never = asyncio.Event()

    async def process(_claim: RcaJobClaim) -> RcaProcessingResult:
        nonlocal started, cancelled
        started = True
        try:
            await never.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        raise AssertionError("the hanging processor must be cancelled")

    handler = RcaJobHandler(
        cast(Any, _Sessions()),
        process,
        worker_id="worker",
        lease_renewal_seconds=1,
    )

    async def fake_claim(
        _handler: RcaJobHandler, _message: RcaJobMessage
    ) -> RcaJobClaim:
        return claim

    async def fake_settle_failure(
        _handler: RcaJobHandler,
        _claim: RcaJobClaim,
        failure_code: LifecycleFailureCode,
    ) -> JobDisposition:
        settled.append(failure_code)
        return JobDisposition.ACK

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(RcaJobHandler, "_claim", fake_claim)
    monkeypatch.setattr(RcaJobHandler, "_settle_failure", fake_settle_failure)

    try:
        assert await handler.handle(message) is JobDisposition.ACK
        assert started is True
        assert cancelled is True
        assert settled == ["DEADLINE_EXCEEDED"]
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_lease_renewal_does_not_open_db_after_deadline() -> None:
    now = datetime.now(UTC)
    claim = RcaJobClaim(
        worker_job_id=uuid4(),
        rca_run_id=uuid4(),
        incident_id=uuid4(),
        attempt_number=1,
        deadline_at=now + timedelta(milliseconds=10),
        lease_owner="worker",
    )

    async def wait_forever() -> RcaProcessingResult:
        await asyncio.Event().wait()
        raise AssertionError("the lease renewal processor must be cancelled")

    processor: asyncio.Task[RcaProcessingResult] = asyncio.create_task(wait_forever())
    handler = RcaJobHandler(
        cast(Any, _Sessions()),
        lambda _claim: _completed(),
        worker_id="worker",
        lease_renewal_seconds=1,
    )
    try:
        with pytest.raises(DeadlineExceededError):
            await handler._renew_lease(claim, processor)
    finally:
        processor.cancel()
        await asyncio.gather(processor, return_exceptions=True)


async def _completed() -> RcaProcessingResult:
    return RcaProcessingResult(status="COMPLETE")
