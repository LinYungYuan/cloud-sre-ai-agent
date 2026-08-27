from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from sre_agent.application.operator.read_models import OperatorIdentity
from sre_agent.application.outbox.publish_events import (
    OutboxEventNotFound,
    OutboxPublishResult,
    PublishResultCode,
)
from sre_agent.application.outbox.recover_events import (
    OutboxRecoveryService,
    SqlAlchemyOutboxRecoveryAuditRepository,
)

EVENT_ID = UUID("60000000-0000-0000-0000-000000000001")
SECOND_EVENT_ID = UUID("60000000-0000-0000-0000-000000000002")
THIRD_EVENT_ID = UUID("60000000-0000-0000-0000-000000000003")
FOURTH_EVENT_ID = UUID("60000000-0000-0000-0000-000000000004")
IDENTITY = OperatorIdentity("operator@example.com", global_access=True)
CORRELATION_ID = "recovery-request-1"


class FakePublisher:
    def __init__(self, results: Sequence[OutboxPublishResult]) -> None:
        self._results = tuple(results)
        self.event_ids: list[UUID] = []
        self.pending_limits: list[int] = []
        self.failed_limits: list[int] = []
        self.error: Exception | None = None

    async def publish_event(self, event_id: UUID) -> OutboxPublishResult:
        self.event_ids.append(event_id)
        if self.error is not None:
            raise self.error
        return self._results[0]

    async def publish_pending(self, limit: int) -> tuple[OutboxPublishResult, ...]:
        self.pending_limits.append(limit)
        return self._results

    async def publish_failed(self, limit: int) -> tuple[OutboxPublishResult, ...]:
        self.failed_limits.append(limit)
        return self._results


class RecordingAuditRepository:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(self, **values: Any) -> None:
        self.records.append(values)


def _result(
    event_id: UUID,
    result: PublishResultCode,
    failure_category: str | None = None,
) -> OutboxPublishResult:
    return OutboxPublishResult(
        event_id=event_id,
        previous_status="PENDING",
        result=result,
        failure_category=failure_category,
    )


@pytest.mark.asyncio
async def test_retry_event_returns_the_durable_publish_result_and_safe_audit() -> None:
    publisher = FakePublisher([_result(EVENT_ID, PublishResultCode.PUBLISHED)])
    audit = RecordingAuditRepository()

    result = await OutboxRecoveryService(publisher, audit).retry_event(
        EVENT_ID,
        IDENTITY,
        CORRELATION_ID,
    )

    assert result == _result(EVENT_ID, PublishResultCode.PUBLISHED)
    assert publisher.event_ids == [EVENT_ID]
    assert audit.records == [
        {
            "action": "outbox.retry_event",
            "identity": IDENTITY,
            "correlation_id": CORRELATION_ID,
            "event_ids": (EVENT_ID,),
            "selected": 1,
            "published": 1,
            "failed": 0,
            "no_op": 0,
            "failure_categories": (),
            "outcome": "PUBLISHED",
        }
    ]


@pytest.mark.asyncio
async def test_retry_event_records_not_found_without_suppressing_the_404_signal() -> None:
    publisher = FakePublisher([])
    publisher.error = OutboxEventNotFound(str(EVENT_ID))
    audit = RecordingAuditRepository()

    with pytest.raises(OutboxEventNotFound):
        await OutboxRecoveryService(publisher, audit).retry_event(
            EVENT_ID,
            IDENTITY,
            CORRELATION_ID,
        )

    assert audit.records == [
        {
            "action": "outbox.retry_event",
            "identity": IDENTITY,
            "correlation_id": CORRELATION_ID,
            "event_ids": (EVENT_ID,),
            "selected": 1,
            "published": 0,
            "failed": 0,
            "no_op": 0,
            "failure_categories": (),
            "outcome": "NOT_FOUND",
        }
    ]


@pytest.mark.asyncio
async def test_retry_pending_summarizes_stable_result_counts_without_event_payloads() -> None:
    publisher = FakePublisher(
        [
            _result(EVENT_ID, PublishResultCode.PUBLISHED),
            _result(SECOND_EVENT_ID, PublishResultCode.FAILED, "PUBLISH_ERROR"),
            _result(THIRD_EVENT_ID, PublishResultCode.FAILED, "INVALID_EVENT"),
            _result(FOURTH_EVENT_ID, PublishResultCode.NO_OP),
        ]
    )
    audit = RecordingAuditRepository()

    result = await OutboxRecoveryService(publisher, audit).retry_pending(
        4,
        IDENTITY,
        CORRELATION_ID,
    )

    assert asdict(result) == {
        "selected": 4,
        "published": 1,
        "failed": 2,
        "no_op": 1,
        "failure_categories": ("INVALID_EVENT", "PUBLISH_ERROR"),
    }
    assert publisher.pending_limits == [4]
    assert audit.records[0]["event_ids"] == (
        EVENT_ID,
        SECOND_EVENT_ID,
        THIRD_EVENT_ID,
        FOURTH_EVENT_ID,
    )
    assert audit.records[0]["failure_categories"] == (
        "INVALID_EVENT",
        "PUBLISH_ERROR",
    )
    assert "payload" not in audit.records[0]


@pytest.mark.asyncio
async def test_retry_failed_delegates_only_to_the_failed_selector() -> None:
    publisher = FakePublisher([_result(EVENT_ID, PublishResultCode.NO_OP)])
    audit = RecordingAuditRepository()

    result = await OutboxRecoveryService(publisher, audit).retry_failed(
        1,
        IDENTITY,
        CORRELATION_ID,
    )

    assert result.no_op == 1
    assert publisher.pending_limits == []
    assert publisher.failed_limits == [1]


class AuditSession:
    def __init__(self, subject_id: UUID | None) -> None:
        self.subject_id = subject_id
        self.scalar_calls: list[tuple[str, dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []

    async def scalar(self, statement, parameters: dict[str, Any]) -> UUID | None:
        self.scalar_calls.append((str(statement), parameters))
        return self.subject_id

    async def execute(self, statement, parameters: dict[str, Any]) -> None:
        self.execute_calls.append((str(statement), parameters))


class AuditTransaction:
    def __init__(self, session: AuditSession) -> None:
        self.session = session

    async def __aenter__(self) -> AuditSession:
        return self.session

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback


class AuditSessionFactory:
    def __init__(self, subject_id: UUID | None) -> None:
        self.session = AuditSession(subject_id)

    def begin(self) -> AuditTransaction:
        return AuditTransaction(self.session)


@pytest.mark.asyncio
async def test_audit_repository_writes_only_the_business_occurred_timestamp() -> None:
    subject_id = UUID("10000000-0000-0000-0000-000000000001")
    database = AuditSessionFactory(subject_id)
    occurred_at = datetime(2026, 8, 27, 12, 30, tzinfo=UTC)
    repository = SqlAlchemyOutboxRecoveryAuditRepository(
        database,  # pyright: ignore[reportArgumentType]
        clock=lambda: occurred_at,
    )

    await repository.record(
        action="outbox.retry_pending",
        identity=IDENTITY,
        correlation_id=CORRELATION_ID,
        event_ids=(EVENT_ID,),
        selected=1,
        published=1,
        failed=0,
        no_op=0,
        failure_categories=(),
        outcome="COMPLETED",
    )

    assert "external_id" in database.session.scalar_calls[0][0]
    insert_sql, parameters = database.session.execute_calls[0]
    assert "partition_timestamp" not in insert_sql
    assert parameters["occurred_at"] == occurred_at
    assert parameters["actor_id"] == subject_id
    assert parameters["scope"] == {
        "correlationId": CORRELATION_ID,
        "eventIds": [str(EVENT_ID)],
        "selected": 1,
        "published": 1,
        "failed": 0,
        "noOp": 0,
        "failureCategories": [],
        "outcome": "COMPLETED",
    }
    assert "payload" not in insert_sql
