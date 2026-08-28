from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sre_agent.application.operator.read_models import OperatorIdentity
from sre_agent.application.outbox.publish_events import (
    OutboxEventNotFound,
    OutboxPublishResult,
    PublishResultCode,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("outbox recovery audit timestamps must be timezone-aware")
    return value.astimezone(UTC)


_INSERT_AUDIT_EVENT = text(
    """
    INSERT INTO audit_events (
        occurred_at,
        actor_id,
        action,
        resource_type,
        resource_id,
        scope
    ) VALUES (
        :occurred_at,
        :actor_id,
        :action,
        'OUTBOX_EVENT',
        :resource_id,
        :scope
    )
    """
).bindparams(bindparam("scope", type_=JSONB))


class OutboxPublisher(Protocol):
    async def publish_event(self, event_id: UUID) -> OutboxPublishResult: ...

    async def publish_pending(self, limit: int) -> tuple[OutboxPublishResult, ...]: ...

    async def publish_failed(self, limit: int) -> tuple[OutboxPublishResult, ...]: ...


class OutboxRecoveryAuditRepository(Protocol):
    async def record(
        self,
        *,
        action: str,
        identity: OperatorIdentity,
        correlation_id: str,
        event_ids: tuple[UUID, ...],
        selected: int,
        published: int,
        failed: int,
        no_op: int,
        failure_categories: tuple[str, ...],
        outcome: str,
    ) -> None: ...


class OutboxRecoveryRequestBodyForbidden(ValueError):
    """Raised when a recovery endpoint receives a request body."""


class SqlAlchemyOutboxRecoveryAuditRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def record(
        self,
        *,
        action: str,
        identity: OperatorIdentity,
        correlation_id: str,
        event_ids: tuple[UUID, ...],
        selected: int,
        published: int,
        failed: int,
        no_op: int,
        failure_categories: tuple[str, ...],
        outcome: str,
    ) -> None:
        occurred_at = _utc(self._clock())
        async with self._session_factory.begin() as session:
            actor_id = await session.scalar(
                text("SELECT id FROM subjects WHERE external_id = :external_id"),
                {"external_id": identity.external_id},
            )
            scope = {
                "correlationId": correlation_id,
                "eventIds": [str(event_id) for event_id in event_ids],
                "selected": selected,
                "published": published,
                "failed": failed,
                "noOp": no_op,
                "failureCategories": list(failure_categories),
                "outcome": outcome,
            }
            if actor_id is None:
                scope["actorExternalId"] = identity.external_id
            await session.execute(
                _INSERT_AUDIT_EVENT,
                {
                    "occurred_at": occurred_at,
                    "actor_id": actor_id,
                    "action": action,
                    "resource_id": event_ids[0] if len(event_ids) == 1 else None,
                    "scope": scope,
                },
            )


@dataclass(frozen=True, slots=True)
class OutboxRecoveryBatchResult:
    selected: int
    published: int
    failed: int
    no_op: int
    failure_categories: tuple[str, ...]


class OutboxRecoveryService:
    def __init__(
        self,
        publisher: OutboxPublisher,
        audit_repository: OutboxRecoveryAuditRepository,
    ) -> None:
        self._publisher = publisher
        self._audit_repository = audit_repository

    async def retry_event(
        self,
        event_id: UUID,
        identity: OperatorIdentity,
        correlation_id: str,
    ) -> OutboxPublishResult:
        try:
            result = await self._publisher.publish_event(event_id)
        except OutboxEventNotFound:
            await self._record(
                action="outbox.retry_event",
                identity=identity,
                correlation_id=correlation_id,
                event_ids=(event_id,),
                selected=1,
                published=0,
                failed=0,
                no_op=0,
                failure_categories=(),
                outcome="NOT_FOUND",
            )
            raise

        summary = _summary((result,))
        await self._record(
            action="outbox.retry_event",
            identity=identity,
            correlation_id=correlation_id,
            event_ids=(event_id,),
            selected=summary.selected,
            published=summary.published,
            failed=summary.failed,
            no_op=summary.no_op,
            failure_categories=summary.failure_categories,
            outcome=result.result.value,
        )
        return result

    async def retry_pending(
        self,
        limit: int,
        identity: OperatorIdentity,
        correlation_id: str,
    ) -> OutboxRecoveryBatchResult:
        results = await self._publisher.publish_pending(limit)
        return await self._record_batch(
            action="outbox.retry_pending",
            results=results,
            identity=identity,
            correlation_id=correlation_id,
        )

    async def retry_failed(
        self,
        limit: int,
        identity: OperatorIdentity,
        correlation_id: str,
    ) -> OutboxRecoveryBatchResult:
        results = await self._publisher.publish_failed(limit)
        return await self._record_batch(
            action="outbox.retry_failed",
            results=results,
            identity=identity,
            correlation_id=correlation_id,
        )

    async def _record_batch(
        self,
        *,
        action: str,
        results: Sequence[OutboxPublishResult],
        identity: OperatorIdentity,
        correlation_id: str,
    ) -> OutboxRecoveryBatchResult:
        summary = _summary(results)
        await self._record(
            action=action,
            identity=identity,
            correlation_id=correlation_id,
            event_ids=tuple(result.event_id for result in results),
            selected=summary.selected,
            published=summary.published,
            failed=summary.failed,
            no_op=summary.no_op,
            failure_categories=summary.failure_categories,
            outcome="COMPLETED",
        )
        return summary

    async def _record(
        self,
        *,
        action: str,
        identity: OperatorIdentity,
        correlation_id: str,
        event_ids: tuple[UUID, ...],
        selected: int,
        published: int,
        failed: int,
        no_op: int,
        failure_categories: tuple[str, ...],
        outcome: str,
    ) -> None:
        await self._audit_repository.record(
            action=action,
            identity=identity,
            correlation_id=correlation_id,
            event_ids=event_ids,
            selected=selected,
            published=published,
            failed=failed,
            no_op=no_op,
            failure_categories=failure_categories,
            outcome=outcome,
        )
        logger.info(
            "outbox_recovery_completed",
            extra={
                "action": action,
                "correlation_id": correlation_id,
                "event_count": len(event_ids),
                "selected": selected,
                "published": published,
                "failed": failed,
                "no_op": no_op,
                "failure_categories": failure_categories,
                "outcome": outcome,
            },
        )


def _summary(results: Sequence[OutboxPublishResult]) -> OutboxRecoveryBatchResult:
    failure_categories = tuple(
        sorted(
            {
                result.failure_category
                for result in results
                if result.failure_category is not None
            }
        )
    )
    return OutboxRecoveryBatchResult(
        selected=len(results),
        published=sum(result.result is PublishResultCode.PUBLISHED for result in results),
        failed=sum(result.result is PublishResultCode.FAILED for result in results),
        no_op=sum(result.result is PublishResultCode.NO_OP for result in results),
        failure_categories=failure_categories,
    )
