from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from sre_agent.application.outbox.publish_events import (
    OutboxEventNotFound,
    OutboxPublishService,
    PublishResultCode,
)

EVENT_ID = UUID("60000000-0000-0000-0000-000000000001")
OTHER_EVENT_ID = UUID("60000000-0000-0000-0000-000000000002")
RESOURCE_ID = UUID("70000000-0000-0000-0000-000000000001")
WORKER_JOB_ID = UUID("80000000-0000-0000-0000-000000000001")
RUN_ID = UUID("90000000-0000-0000-0000-000000000001")
CREATED_AT = datetime(2026, 8, 13, 1, 2, 3, tzinfo=UTC)
NOW = datetime(2026, 8, 13, 2, 0, tzinfo=UTC)
TOPIC = "projects/sre-agent/topics/rca-jobs"


def _row(
    *,
    event_id: UUID = EVENT_ID,
    status: str = "PENDING",
    event_type: str = "RCA_RUN_REQUESTED",
    payload: object | None = None,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "aggregate_id": RESOURCE_ID,
        "event_type": event_type,
        "payload": payload
        if payload is not None
        else {
            "schemaVersion": 1,
            "workerJobId": str(WORKER_JOB_ID),
            "rcaRunId": str(RUN_ID),
            "incidentId": str(RESOURCE_ID),
            "attempt": 1,
        },
        "idempotency_key": f"rca-run:{RUN_ID}",
        "status": status,
        "available_at": CREATED_AT,
        "created_at": CREATED_AT,
        "published_at": NOW if status == "PUBLISHED" else None,
    }


class _MappingsResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingsResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def one_or_none(self) -> dict[str, Any] | None:
        if not self._rows:
            return None
        if len(self._rows) != 1:
            raise AssertionError("expected at most one row")
        return self._rows[0]


class _FakeSession:
    def __init__(self, database: _FakeSessionFactory) -> None:
        self._database = database

    async def execute(self, statement, parameters: Mapping[str, Any]):
        sql = str(statement)
        self._database.statements.append((sql, dict(parameters)))
        if sql.lstrip().startswith("SELECT"):
            if "event_id" in parameters:
                event_id = parameters["event_id"]
                row = self._database.rows.get(event_id)
                return _MappingsResult([] if row is None else [dict(row)])
            rows = [
                row
                for row in self._database.rows.values()
                if row["status"] == parameters["status"]
                and row["published_at"] is None
                and (
                    "available_at <= :attempted_at" not in sql
                    or row["available_at"] <= parameters["attempted_at"]
                )
            ]
            rows.sort(key=lambda row: (row["available_at"], row["created_at"], row["id"]))
            return _MappingsResult(
                [dict(row) for row in rows[: parameters["limit"]]]
            )
        row = self._database.rows[parameters["event_id"]]
        if "status = 'PUBLISHED'" in sql:
            row["status"] = "PUBLISHED"
            row["published_at"] = parameters["published_at"]
        elif "status = 'FAILED'" in sql:
            row["status"] = "FAILED"
            row["published_at"] = None
            row["available_at"] = parameters["available_at"]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")
        return _MappingsResult([])


class _Transaction:
    def __init__(self, database: _FakeSessionFactory) -> None:
        self._database = database

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession(self._database)

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        del exc_value, traceback
        self._database.committed = exc_type is None


class _FakeSessionFactory:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {row["id"]: row for row in rows}
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.committed = False

    def begin(self) -> _Transaction:
        return _Transaction(self)


class _RecordingPublisher:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def publish(
        self, topic: str, data: bytes, attributes: Mapping[str, str]
    ) -> str:
        self.calls.append((topic, data, dict(attributes)))
        if self.error is not None:
            raise self.error
        return "message-id"


class _BlockingPublisher(_RecordingPublisher):
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


def _service(database: _FakeSessionFactory, publisher: _RecordingPublisher):
    return OutboxPublishService(
        database,
        publisher,
        TOPIC,
        retry_delay=timedelta(seconds=30),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_publish_event_locks_only_the_explicit_uuid_and_publishes_payload():
    database = _FakeSessionFactory([_row(), _row(event_id=OTHER_EVENT_ID)])
    publisher = _RecordingPublisher()

    result = await _service(database, publisher).publish_event(EVENT_ID)

    assert result.event_id == EVENT_ID
    assert result.previous_status == "PENDING"
    assert result.result is PublishResultCode.PUBLISHED
    assert result.failure_category is None
    assert database.rows[EVENT_ID]["status"] == "PUBLISHED"
    assert database.rows[OTHER_EVENT_ID]["status"] == "PENDING"
    select_sql, select_parameters = database.statements[0]
    assert "WHERE id = :event_id" in select_sql
    assert "FOR UPDATE" in select_sql
    assert select_parameters["event_id"] == EVENT_ID
    assert publisher.calls == [
        (
            TOPIC,
            (
                b'{"attempt":1,'
                b'"incidentId":"70000000-0000-0000-0000-000000000001",'
                b'"rcaRunId":"90000000-0000-0000-0000-000000000001",'
                b'"schemaVersion":1,'
                b'"workerJobId":"80000000-0000-0000-0000-000000000001"}'
            ),
            {"idempotencyKey": f"rca-run:{RUN_ID}"},
        )
    ]


@pytest.mark.asyncio
async def test_publish_event_raises_stable_not_found_for_unknown_uuid():
    database = _FakeSessionFactory([])

    with pytest.raises(OutboxEventNotFound):
        await _service(database, _RecordingPublisher()).publish_event(EVENT_ID)


@pytest.mark.asyncio
async def test_published_event_is_an_idempotent_no_op():
    database = _FakeSessionFactory([_row(status="PUBLISHED")])
    publisher = _RecordingPublisher()

    result = await _service(database, publisher).publish_event(EVENT_ID)

    assert result.previous_status == "PUBLISHED"
    assert result.result is PublishResultCode.NO_OP
    assert result.failure_category is None
    assert publisher.calls == []


@pytest.mark.asyncio
async def test_unsupported_event_type_is_durably_failed_without_escaping():
    database = _FakeSessionFactory([_row(event_type="UNSUPPORTED_EVENT")])
    publisher = _RecordingPublisher()

    result = await _service(database, publisher).publish_event(EVENT_ID)

    assert result.result is PublishResultCode.FAILED
    assert result.failure_category == "INVALID_EVENT"
    assert database.rows[EVENT_ID]["status"] == "FAILED"
    assert database.rows[EVENT_ID]["available_at"] == NOW + timedelta(seconds=30)
    assert database.committed is True
    assert publisher.calls == []


@pytest.mark.asyncio
async def test_malformed_persisted_payload_is_durably_failed_without_escaping():
    database = _FakeSessionFactory([_row(payload={"secret": "not-a-job"})])
    publisher = _RecordingPublisher()

    result = await _service(database, publisher).publish_event(EVENT_ID)

    assert result.result is PublishResultCode.FAILED
    assert result.failure_category == "INVALID_EVENT"
    assert database.rows[EVENT_ID]["status"] == "FAILED"
    assert database.committed is True
    assert publisher.calls == []


@pytest.mark.asyncio
async def test_transport_error_is_durably_failed_without_leaking_exception():
    secret = "token=must-not-reach-webhook-caller"
    database = _FakeSessionFactory([_row()])
    publisher = _RecordingPublisher(RuntimeError(secret))

    result = await _service(database, publisher).publish_event(EVENT_ID)

    assert result.result is PublishResultCode.FAILED
    assert result.failure_category == "PUBLISH_ERROR"
    assert secret not in repr(result)
    assert database.rows[EVENT_ID]["status"] == "FAILED"
    assert database.committed is True


@pytest.mark.asyncio
async def test_outer_cancellation_waits_for_durable_settlement_then_propagates():
    database = _FakeSessionFactory([_row()])
    publisher = _BlockingPublisher()
    publishing = asyncio.create_task(
        _service(database, publisher).publish_event(EVENT_ID)
    )
    assert await asyncio.to_thread(publisher.started.wait, 2)

    publishing.cancel()
    await asyncio.sleep(0)
    assert not publishing.done()
    publisher.release.set()

    with pytest.raises(asyncio.CancelledError):
        await publishing
    assert database.committed is True
    assert database.rows[EVENT_ID]["status"] == "PUBLISHED"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 101])
async def test_batch_limit_must_be_between_one_and_one_hundred(limit: int):
    service = _service(_FakeSessionFactory([]), _RecordingPublisher())

    with pytest.raises(ValueError, match="limit must be between 1 and 100"):
        await service.publish_pending(limit)


@pytest.mark.asyncio
async def test_failed_batch_can_retry_before_the_previous_backoff_deadline():
    database = _FakeSessionFactory(
        [_row(status="FAILED") | {"available_at": NOW + timedelta(hours=1)}]
    )
    publisher = _RecordingPublisher()

    results = await _service(database, publisher).publish_failed(limit=1)

    assert [result.event_id for result in results] == [EVENT_ID]
    assert results[0].result is PublishResultCode.PUBLISHED
