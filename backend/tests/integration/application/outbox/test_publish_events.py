from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sre_agent.application.outbox.publish_events import (
    OutboxPublishService,
    PublishResultCode,
)

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
).replace("postgresql://", "postgresql+asyncpg://", 1)
RESOURCE_ID = UUID("70000000-0000-0000-0000-000000000001")
WORKER_JOB_ID = UUID("80000000-0000-0000-0000-000000000001")
RUN_ID = UUID("90000000-0000-0000-0000-000000000001")
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
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE outbox_events"))
        await engine.dispose()


async def _insert_event(
    session_factory: async_sessionmaker[AsyncSession],
    event_id: UUID,
    *,
    status: str = "PENDING",
    available_at: datetime = NOW - timedelta(minutes=1),
    created_at: datetime = NOW - timedelta(minutes=1),
) -> None:
    async with session_factory.begin() as session:
        await session.execute(
            text(
                """
                INSERT INTO outbox_events (
                    id, aggregate_type, aggregate_id, event_type, payload,
                    idempotency_key, status, available_at, created_at
                ) VALUES (
                    :event_id, 'INCIDENT', :resource_id, 'RCA_RUN_REQUESTED',
                    CAST(:payload AS jsonb), :idempotency_key, :status,
                    :available_at, :created_at
                )
                """
            ),
            {
                "event_id": event_id,
                "resource_id": RESOURCE_ID,
                "payload": json.dumps(
                    {
                        "schemaVersion": 1,
                        "workerJobId": str(WORKER_JOB_ID),
                        "rcaRunId": str(event_id),
                        "incidentId": str(RESOURCE_ID),
                        "attempt": 1,
                    }
                ),
                "idempotency_key": f"rca-run:{event_id}",
                "status": status,
                "available_at": available_at,
                "created_at": created_at,
            },
        )


async def _status(
    session_factory: async_sessionmaker[AsyncSession], event_id: UUID
) -> str:
    async with session_factory() as session:
        value = await session.scalar(
            text("SELECT status FROM outbox_events WHERE id = :event_id"),
            {"event_id": event_id},
        )
    assert isinstance(value, str)
    return value


class RecordingPublisher:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def publish(
        self, topic: str, data: bytes, attributes: Mapping[str, str]
    ) -> str:
        with self._lock:
            self.calls.append((topic, data, dict(attributes)))
        return "message-id"


class BlockingPublisher(RecordingPublisher):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def publish(
        self, topic: str, data: bytes, attributes: Mapping[str, str]
    ) -> str:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release publisher")
        return super().publish(topic, data, attributes)


def _service(session_factory, publisher):
    return OutboxPublishService(
        session_factory,
        publisher,
        TOPIC,
        retry_delay=timedelta(seconds=30),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_explicit_publish_holds_row_lock_until_ack_and_settlement(
    session_factory,
):
    event_id = UUID("60000000-0000-0000-0000-000000000001")
    await _insert_event(session_factory, event_id)
    publisher = BlockingPublisher()
    service = _service(session_factory, publisher)

    first = asyncio.create_task(service.publish_event(event_id))
    assert await asyncio.to_thread(publisher.started.wait, 2)
    second = asyncio.create_task(service.publish_event(event_id))
    await asyncio.sleep(0.1)
    assert not second.done()

    publisher.release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result.result is PublishResultCode.PUBLISHED
    assert second_result.previous_status == "PUBLISHED"
    assert second_result.result is PublishResultCode.NO_OP
    assert len(publisher.calls) == 1


@pytest.mark.asyncio
async def test_pending_and_failed_batches_are_status_specific(session_factory):
    pending_id = UUID("60000000-0000-0000-0000-000000000001")
    failed_id = UUID("60000000-0000-0000-0000-000000000002")
    await _insert_event(session_factory, pending_id)
    await _insert_event(
        session_factory,
        failed_id,
        status="FAILED",
        available_at=NOW + timedelta(hours=1),
    )
    publisher = RecordingPublisher()
    service = _service(session_factory, publisher)

    pending_results = await service.publish_pending(limit=100)
    assert [result.event_id for result in pending_results] == [pending_id]
    assert await _status(session_factory, pending_id) == "PUBLISHED"
    assert await _status(session_factory, failed_id) == "FAILED"

    failed_results = await service.publish_failed(limit=100)
    assert [result.event_id for result in failed_results] == [failed_id]
    assert await _status(session_factory, failed_id) == "PUBLISHED"


@pytest.mark.asyncio
async def test_batch_selection_uses_stable_available_created_id_order(
    session_factory,
):
    first_id = UUID("60000000-0000-0000-0000-000000000001")
    second_id = UUID("60000000-0000-0000-0000-000000000002")
    third_id = UUID("60000000-0000-0000-0000-000000000003")
    await _insert_event(
        session_factory,
        third_id,
        available_at=NOW - timedelta(minutes=1),
        created_at=NOW - timedelta(minutes=1),
    )
    await _insert_event(
        session_factory,
        second_id,
        available_at=NOW - timedelta(minutes=2),
        created_at=NOW - timedelta(minutes=1),
    )
    await _insert_event(
        session_factory,
        first_id,
        available_at=NOW - timedelta(minutes=2),
        created_at=NOW - timedelta(minutes=2),
    )

    results = await _service(session_factory, RecordingPublisher()).publish_pending(3)

    assert [result.event_id for result in results] == [first_id, second_id, third_id]


@pytest.mark.asyncio
async def test_batch_skips_locked_rows_without_looping_back(session_factory):
    locked_id = UUID("60000000-0000-0000-0000-000000000001")
    available_id = UUID("60000000-0000-0000-0000-000000000002")
    await _insert_event(session_factory, locked_id, created_at=NOW - timedelta(minutes=2))
    await _insert_event(
        session_factory, available_id, created_at=NOW - timedelta(minutes=1)
    )
    publisher = RecordingPublisher()

    async with session_factory.begin() as locking_session:
        await locking_session.execute(
            text(
                "SELECT id FROM outbox_events "
                "WHERE id = :event_id FOR UPDATE"
            ),
            {"event_id": locked_id},
        )
        results = await asyncio.wait_for(
            _service(session_factory, publisher).publish_pending(2), timeout=2
        )
        assert [result.event_id for result in results] == [available_id]
        assert await _status(session_factory, locked_id) == "PENDING"

    assert [call[2]["idempotencyKey"] for call in publisher.calls] == [
        f"rca-run:{available_id}"
    ]
