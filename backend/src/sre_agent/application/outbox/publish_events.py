from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import perf_counter
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sre_agent.integrations.pubsub.messages import RcaJobMessage
from sre_agent.integrations.pubsub.publisher import MessagePublisher

_DEFERRED_CANCELLATION_NOTE = (
    "Outer cancellation was deferred during durable outbox settlement."
)
_INVALID_EVENT = "INVALID_EVENT"
_PUBLISH_ERROR = "PUBLISH_ERROR"
_T = TypeVar("_T")
logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("outbox datetimes must be timezone-aware")
    return value.astimezone(UTC)


class PublishResultCode(StrEnum):
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    NO_OP = "NO_OP"


@dataclass(frozen=True, slots=True)
class OutboxPublishResult:
    event_id: UUID
    previous_status: str
    result: PublishResultCode
    failure_category: str | None = None


class OutboxEventNotFound(LookupError):
    """The requested durable outbox event does not exist."""


class _InvalidPersistedEvent(ValueError):
    pass


def _published_data(row: Mapping[str, Any]) -> bytes:
    if row["event_type"] != "RCA_RUN_REQUESTED":
        raise _InvalidPersistedEvent("unsupported persisted event type")
    payload = row["payload"]
    if not isinstance(payload, Mapping):
        raise _InvalidPersistedEvent("persisted event payload is not an object")
    try:
        return RcaJobMessage.from_mapping(payload).to_bytes()
    except (TypeError, ValueError) as error:
        raise _InvalidPersistedEvent("invalid persisted RCA job message") from error


class OutboxPublishService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: MessagePublisher,
        topic: str,
        *,
        retry_delay: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if retry_delay <= timedelta(0):
            raise ValueError("retry_delay must be positive")
        self._session_factory = session_factory
        self._publisher = publisher
        self._topic = topic
        self._retry_delay = retry_delay
        self._clock = clock

    async def publish_event(self, event_id: UUID) -> OutboxPublishResult:
        return await self._complete_durable_settlement(
            self._publish_event_and_settle(event_id)
        )

    async def publish_pending(
        self, limit: int
    ) -> tuple[OutboxPublishResult, ...]:
        return await self._publish_status("PENDING", limit)

    async def publish_failed(self, limit: int) -> tuple[OutboxPublishResult, ...]:
        return await self._publish_status("FAILED", limit)

    async def _publish_status(
        self, status: str, limit: int
    ) -> tuple[OutboxPublishResult, ...]:
        self._validate_limit(limit)
        return await self._complete_durable_settlement(
            self._publish_status_and_settle(status, limit)
        )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

    async def _complete_durable_settlement(
        self, operation: Coroutine[Any, Any, _T]
    ) -> _T:
        settlement = asyncio.create_task(operation)
        cancellation_requested = False
        while True:
            try:
                result = await asyncio.shield(settlement)
            except asyncio.CancelledError:
                if settlement.cancelled():
                    raise
                cancellation_requested = True
                if settlement.done():
                    try:
                        result = settlement.result()
                    except Exception as error:
                        error.add_note(_DEFERRED_CANCELLATION_NOTE)
                        raise
                    break
                continue
            except Exception as error:
                if cancellation_requested:
                    error.add_note(_DEFERRED_CANCELLATION_NOTE)
                raise
            break

        if cancellation_requested:
            raise asyncio.CancelledError
        return result

    async def _publish_event_and_settle(
        self, event_id: UUID
    ) -> OutboxPublishResult:
        attempted_at = _utc(self._clock())
        async with self._session_factory.begin() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id, aggregate_id, event_type, payload,
                           idempotency_key, status, available_at,
                           published_at, created_at
                    FROM outbox_events
                    WHERE id = :event_id
                    FOR UPDATE
                    """
                ),
                {"event_id": event_id},
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise OutboxEventNotFound(str(event_id))
            results = await self._publish_locked_rows(
                session, (dict(row),), attempted_at
            )
        return results[0]

    async def _publish_status_and_settle(
        self, status: str, limit: int
    ) -> tuple[OutboxPublishResult, ...]:
        attempted_at = _utc(self._clock())
        async with self._session_factory.begin() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id, aggregate_id, event_type, payload,
                           idempotency_key, status, available_at,
                           published_at, created_at
                    FROM outbox_events
                    WHERE status = :status
                      AND published_at IS NULL
                    ORDER BY available_at, created_at, id
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                    """
                ),
                {
                    "status": status,
                    "limit": limit,
                },
            )
            rows = [dict(row) for row in result.mappings().all()]
            return await self._publish_locked_rows(session, rows, attempted_at)

    async def _publish_locked_rows(
        self,
        session: AsyncSession,
        rows: Sequence[dict[str, Any]],
        attempted_at: datetime,
    ) -> tuple[OutboxPublishResult, ...]:
        results: list[OutboxPublishResult] = []
        for row in rows:
            started_at = perf_counter()
            event_id = row["id"]
            previous_status = row["status"]
            if previous_status == "PUBLISHED":
                publish_result = OutboxPublishResult(
                    event_id,
                    previous_status,
                    PublishResultCode.NO_OP,
                )
            else:
                try:
                    data = _published_data(row)
                except _InvalidPersistedEvent:
                    publish_result = await self._settle_failed(
                        session,
                        event_id,
                        previous_status,
                        attempted_at,
                        _INVALID_EVENT,
                    )
                else:
                    try:
                        await asyncio.to_thread(
                            self._publisher.publish,
                            self._topic,
                            data,
                            {"idempotencyKey": row["idempotency_key"]},
                        )
                    except Exception:  # noqa: BLE001 -- stable boundary result
                        publish_result = await self._settle_failed(
                            session,
                            event_id,
                            previous_status,
                            attempted_at,
                            _PUBLISH_ERROR,
                        )
                    else:
                        await session.execute(
                            text(
                                """
                                UPDATE outbox_events
                                SET status = 'PUBLISHED',
                                    published_at = :published_at
                                WHERE id = :event_id
                                """
                            ),
                            {
                                "event_id": event_id,
                                "published_at": attempted_at,
                            },
                        )
                        publish_result = OutboxPublishResult(
                            event_id,
                            previous_status,
                            PublishResultCode.PUBLISHED,
                        )
            self._log_result(publish_result, started_at)
            results.append(publish_result)
        return tuple(results)

    async def _settle_failed(
        self,
        session: AsyncSession,
        event_id: UUID,
        previous_status: str,
        attempted_at: datetime,
        failure_category: str,
    ) -> OutboxPublishResult:
        await session.execute(
            text(
                """
                UPDATE outbox_events
                SET status = 'FAILED', published_at = NULL,
                    available_at = :available_at
                WHERE id = :event_id
                """
            ),
            {
                "event_id": event_id,
                "available_at": attempted_at + self._retry_delay,
            },
        )
        return OutboxPublishResult(
            event_id,
            previous_status,
            PublishResultCode.FAILED,
            failure_category,
        )

    @staticmethod
    def _log_result(result: OutboxPublishResult, started_at: float) -> None:
        logger.info(
            "outbox_publish_settled",
            extra={
                "event_id": str(result.event_id),
                "result": result.result.value,
                "failure_category": result.failure_category,
                "latency_ms": round((perf_counter() - started_at) * 1000, 3),
            },
        )
