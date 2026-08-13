from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sre_agent.integrations.pubsub.publisher import MessagePublisher

_DEFERRED_CANCELLATION_NOTE = (
    "Outer cancellation was deferred during durable outbox settlement."
)


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("outbox datetimes must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_event(row: dict[str, Any]) -> bytes:
    occurred_at = _utc(row["created_at"])
    envelope = {
        "eventId": str(row["id"]),
        "eventType": row["event_type"],
        "resourceId": str(row["aggregate_id"]),
        "occurredAt": occurred_at.isoformat().replace("+00:00", "Z"),
        "version": 1,
    }
    return json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class OutboxPublisher:
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

    async def publish_batch(self, limit: int) -> int:
        if limit <= 0:
            raise ValueError("limit must be positive")

        settlement = asyncio.create_task(self._publish_and_settle_batch(limit))
        cancellation_requested = False
        while True:
            try:
                published = await asyncio.shield(settlement)
            except asyncio.CancelledError:
                if settlement.cancelled():
                    raise
                cancellation_requested = True
                if settlement.done():
                    try:
                        published = settlement.result()
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
        return published

    async def _publish_and_settle_batch(self, limit: int) -> int:
        attempted_at = _utc(self._clock())
        published = 0
        async with self._session_factory.begin() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id, aggregate_id, event_type, idempotency_key, created_at
                    FROM outbox_events
                    WHERE status IN ('PENDING', 'FAILED')
                      AND published_at IS NULL
                      AND available_at <= :attempted_at
                    ORDER BY available_at, created_at, id
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                    """
                ),
                {"attempted_at": attempted_at, "limit": limit},
            )
            rows = [dict(row) for row in result.mappings().all()]

            for row in rows:
                try:
                    await asyncio.to_thread(
                        self._publisher.publish,
                        self._topic,
                        _canonical_event(row),
                        {"idempotencyKey": row["idempotency_key"]},
                    )
                except Exception:  # noqa: BLE001 -- publisher failures are retryable
                    await session.execute(
                        text(
                            """
                            UPDATE outbox_events
                            SET status = 'FAILED', available_at = :available_at
                            WHERE id = :id
                            """
                        ),
                        {
                            "id": row["id"],
                            "available_at": attempted_at + self._retry_delay,
                        },
                    )
                else:
                    await session.execute(
                        text(
                            """
                            UPDATE outbox_events
                            SET status = 'PUBLISHED', published_at = :published_at
                            WHERE id = :id
                            """
                        ),
                        {"id": row["id"], "published_at": attempted_at},
                    )
                    published += 1

        return published
