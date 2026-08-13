from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sre_agent.integrations.pubsub.publisher import GooglePubSubPublisher
from sre_agent.workers.outbox_worker import OutboxPublisher

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/sre_agent",
).replace("postgresql://", "postgresql+asyncpg://", 1)
EVENT_ID = UUID("60000000-0000-0000-0000-000000000001")
RESOURCE_ID = UUID("70000000-0000-0000-0000-000000000001")
CREATED_AT = datetime(2026, 8, 13, 1, 2, 3, 456789, tzinfo=UTC)
NOW = datetime(2026, 8, 13, 2, 0, tzinfo=UTC)
TOPIC = "projects/sre-agent/topics/rca-jobs"


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE outbox_events"))
    try:
        yield factory
    finally:
        await engine.dispose()


async def _insert_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_id: UUID = EVENT_ID,
    resource_id: UUID = RESOURCE_ID,
    idempotency_key: str = "rca-run:90000000-0000-0000-0000-000000000001",
    available_at: datetime = CREATED_AT,
    created_at: datetime = CREATED_AT,
) -> None:
    async with session_factory.begin() as session:
        await session.execute(
            text(
                """
                INSERT INTO outbox_events (
                    id, aggregate_type, aggregate_id, event_type, payload,
                    idempotency_key, status, available_at, created_at
                ) VALUES (
                    :id, 'INCIDENT', :resource_id, 'RCA_RUN_REQUESTED',
                    CAST(:payload AS jsonb), :idempotency_key, 'PENDING',
                    :available_at, :created_at
                )
                """
            ),
            {
                "id": event_id,
                "resource_id": resource_id,
                "payload": json.dumps(
                    {
                        "incident_id": str(resource_id),
                        "rca_run_id": idempotency_key.removeprefix("rca-run:"),
                    }
                ),
                "idempotency_key": idempotency_key,
                "available_at": available_at,
                "created_at": created_at,
            },
        )


async def _event_row(
    session_factory: async_sessionmaker[AsyncSession], event_id: UUID = EVENT_ID
) -> Mapping[str, Any]:
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT * FROM outbox_events WHERE id = :id"), {"id": event_id}
        )
        row = result.mappings().one()
        return dict(row)


class RecordingPublisher:
    def __init__(self, outcomes: list[str | BaseException] | None = None) -> None:
        self._outcomes = list(outcomes or [])
        self._lock = threading.Lock()
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def publish(
        self, topic: str, data: bytes, attributes: Mapping[str, str]
    ) -> str:
        with self._lock:
            self.calls.append((topic, data, dict(attributes)))
            outcome = self._outcomes.pop(0) if self._outcomes else "message-ack"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class BlockingPublisher(RecordingPublisher):
    def __init__(self, outcomes: list[str | BaseException] | None = None) -> None:
        super().__init__(outcomes)
        self.started = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()

    def publish(
        self, topic: str, data: bytes, attributes: Mapping[str, str]
    ) -> str:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release publisher acknowledgement")
        try:
            return super().publish(topic, data, attributes)
        finally:
            self.completed.set()


@pytest.mark.asyncio
async def test_event_is_published_only_after_publisher_acknowledges(session_factory):
    await _insert_event(session_factory)
    publisher = BlockingPublisher()
    worker = OutboxPublisher(session_factory, publisher, TOPIC, clock=lambda: NOW)

    publishing = asyncio.create_task(worker.publish_batch(limit=1))
    assert await asyncio.to_thread(publisher.started.wait, 2)
    before_ack = await _event_row(session_factory)
    assert before_ack["status"] == "PENDING"
    assert before_ack["published_at"] is None

    publisher.release.set()
    assert await publishing == 1
    after_ack = await _event_row(session_factory)
    assert after_ack["status"] == "PUBLISHED"
    assert after_ack["published_at"] == NOW


@pytest.mark.asyncio
async def test_failed_publish_is_durably_scheduled_without_sensitive_error(
    session_factory,
):
    await _insert_event(session_factory)
    publisher = RecordingPublisher([RuntimeError("token=super-secret credential")])
    worker = OutboxPublisher(
        session_factory,
        publisher,
        TOPIC,
        retry_delay=timedelta(seconds=30),
        clock=lambda: NOW,
    )

    assert await worker.publish_batch(limit=1) == 0

    row = await _event_row(session_factory)
    assert row["status"] == "FAILED"
    assert row["published_at"] is None
    assert row["available_at"] == NOW + timedelta(seconds=30)
    assert "super-secret" not in json.dumps(row, default=str)


@pytest.mark.asyncio
async def test_retry_uses_the_same_outbox_idempotency_key(session_factory):
    await _insert_event(session_factory)
    current_time = NOW
    publisher = RecordingPublisher([RuntimeError("temporary"), "ack-on-retry"])
    worker = OutboxPublisher(
        session_factory,
        publisher,
        TOPIC,
        retry_delay=timedelta(seconds=30),
        clock=lambda: current_time,
    )

    assert await worker.publish_batch(limit=1) == 0
    current_time = NOW + timedelta(seconds=30)
    assert await worker.publish_batch(limit=1) == 1

    assert [call[2]["idempotencyKey"] for call in publisher.calls] == [
        "rca-run:90000000-0000-0000-0000-000000000001",
        "rca-run:90000000-0000-0000-0000-000000000001",
    ]


@pytest.mark.asyncio
async def test_publish_uses_exact_canonical_json_envelope(session_factory):
    await _insert_event(session_factory)
    publisher = RecordingPublisher()
    worker = OutboxPublisher(session_factory, publisher, TOPIC, clock=lambda: NOW)

    assert await worker.publish_batch(limit=1) == 1

    assert publisher.calls == [
        (
            TOPIC,
            (
                b'{"eventId":"60000000-0000-0000-0000-000000000001",'
                b'"eventType":"RCA_RUN_REQUESTED",'
                b'"occurredAt":"2026-08-13T01:02:03.456789Z",'
                b'"resourceId":"70000000-0000-0000-0000-000000000001",'
                b'"version":1}'
            ),
            {
                "idempotencyKey": (
                    "rca-run:90000000-0000-0000-0000-000000000001"
                )
            },
        )
    ]


@pytest.mark.asyncio
async def test_two_sessions_skip_a_row_already_claimed_by_another_publisher(
    session_factory,
):
    await _insert_event(session_factory)
    first_publisher = BlockingPublisher()
    second_publisher = RecordingPublisher()
    first = OutboxPublisher(
        session_factory, first_publisher, TOPIC, clock=lambda: NOW
    )
    second = OutboxPublisher(
        session_factory, second_publisher, TOPIC, clock=lambda: NOW
    )

    first_task = asyncio.create_task(first.publish_batch(limit=1))
    assert await asyncio.to_thread(first_publisher.started.wait, 2)
    assert await asyncio.wait_for(second.publish_batch(limit=1), timeout=2) == 0
    first_publisher.release.set()
    assert await first_task == 1

    assert len(first_publisher.calls) == 1
    assert second_publisher.calls == []


@pytest.mark.asyncio
async def test_limit_is_enforced_and_non_positive_limits_are_rejected(session_factory):
    await _insert_event(session_factory)
    await _insert_event(
        session_factory,
        event_id=UUID("60000000-0000-0000-0000-000000000002"),
        resource_id=UUID("70000000-0000-0000-0000-000000000002"),
        idempotency_key="rca-run:90000000-0000-0000-0000-000000000002",
    )
    publisher = RecordingPublisher()
    worker = OutboxPublisher(session_factory, publisher, TOPIC, clock=lambda: NOW)

    with pytest.raises(ValueError, match="limit must be positive"):
        await worker.publish_batch(limit=0)
    with pytest.raises(ValueError, match="limit must be positive"):
        await worker.publish_batch(limit=-1)
    assert await worker.publish_batch(limit=1) == 1
    assert len(publisher.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_publish_is_not_swallowed_or_marked_failed(session_factory):
    await _insert_event(session_factory)
    publisher = RecordingPublisher([asyncio.CancelledError()])
    worker = OutboxPublisher(session_factory, publisher, TOPIC, clock=lambda: NOW)

    with pytest.raises(asyncio.CancelledError):
        await worker.publish_batch(limit=1)

    row = await _event_row(session_factory)
    assert row["status"] == "PENDING"
    assert row["available_at"] == CREATED_AT
    assert row["published_at"] is None


@pytest.mark.asyncio
async def test_outer_cancellation_waits_for_ack_commit_and_keeps_row_claimed(
    session_factory,
):
    await _insert_event(session_factory)
    blocking_publisher = BlockingPublisher()
    duplicate_publisher = RecordingPublisher()
    worker = OutboxPublisher(
        session_factory, blocking_publisher, TOPIC, clock=lambda: NOW
    )
    duplicate_worker = OutboxPublisher(
        session_factory, duplicate_publisher, TOPIC, clock=lambda: NOW
    )

    publishing = asyncio.create_task(worker.publish_batch(limit=1))
    assert await asyncio.to_thread(blocking_publisher.started.wait, 2)
    try:
        publishing.cancel()
        await asyncio.sleep(0)
        publishing.cancel()
        await asyncio.sleep(0)

        assert not publishing.done()
        assert await duplicate_worker.publish_batch(limit=1) == 0
        assert duplicate_publisher.calls == []
    finally:
        blocking_publisher.release.set()

    with pytest.raises(asyncio.CancelledError):
        await publishing
    assert blocking_publisher.completed.is_set()
    row = await _event_row(session_factory)
    assert row["status"] == "PUBLISHED"
    assert row["published_at"] == NOW
    assert await duplicate_worker.publish_batch(limit=1) == 0
    assert len(blocking_publisher.calls) == 1
    assert duplicate_publisher.calls == []


@pytest.mark.asyncio
async def test_outer_cancellation_waits_for_failed_attempt_commit(session_factory):
    await _insert_event(session_factory)
    publisher = BlockingPublisher(
        [RuntimeError("token=must-not-be-persisted-after-cancellation")]
    )
    worker = OutboxPublisher(
        session_factory,
        publisher,
        TOPIC,
        retry_delay=timedelta(seconds=30),
        clock=lambda: NOW,
    )

    publishing = asyncio.create_task(worker.publish_batch(limit=1))
    assert await asyncio.to_thread(publisher.started.wait, 2)
    publishing.cancel()
    await asyncio.sleep(0)
    assert not publishing.done()
    publisher.release.set()

    with pytest.raises(asyncio.CancelledError):
        await publishing
    row = await _event_row(session_factory)
    assert row["status"] == "FAILED"
    assert row["published_at"] is None
    assert row["available_at"] == NOW + timedelta(seconds=30)
    assert "must-not-be-persisted" not in json.dumps(row, default=str)


class StubFuture:
    def result(self) -> str:
        return "google-message-id"


class StubGoogleClient:
    def __init__(self) -> None:
        self.call: tuple[str, bytes, dict[str, str]] | None = None

    def publish(self, topic: str, data: bytes, **attributes: str) -> StubFuture:
        self.call = (topic, data, attributes)
        return StubFuture()


def test_google_adapter_uses_injected_client_and_waits_for_acknowledgement():
    client = StubGoogleClient()
    publisher = GooglePubSubPublisher(client)

    message_id = publisher.publish(TOPIC, b"payload", {"traceId": "trace-1"})

    assert message_id == "google-message-id"
    assert client.call == (TOPIC, b"payload", {"traceId": "trace-1"})


class SettlementDatabaseError(RuntimeError):
    pass


class ControlledFailingSettlement(OutboxPublisher):
    def __init__(self, session_factory) -> None:
        super().__init__(session_factory, RecordingPublisher(), TOPIC)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def _publish_and_settle_batch(self, limit: int) -> int:
        self.started.set()
        await self.release.wait()
        raise SettlementDatabaseError("transaction commit failed")


@pytest.mark.asyncio
async def test_settlement_database_failure_wins_outer_cancellation_done_race(
    session_factory,
):
    worker = ControlledFailingSettlement(session_factory)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        publishing = asyncio.create_task(worker.publish_batch(limit=1))
        await worker.started.wait()
        worker.release.set()
        publishing.cancel()

        with pytest.raises(SettlementDatabaseError) as raised:
            await publishing
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert str(raised.value) == "transaction commit failed"
    assert raised.value.__notes__ == [
        "Outer cancellation was deferred during durable outbox settlement."
    ]
    assert unhandled == []
