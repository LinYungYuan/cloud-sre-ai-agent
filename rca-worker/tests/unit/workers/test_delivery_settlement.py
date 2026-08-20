from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from sre_rca_worker.application.rca.job_lifecycle import JobDisposition
from sre_rca_worker.integrations.pubsub.messages import RcaJobMessage
from sre_rca_worker.integrations.pubsub.subscriber import PubSubDelivery
from sre_rca_worker.workers.rca_worker import settle_delivery


@pytest.mark.asyncio
async def test_delivery_acks_only_after_durable_handler_returns() -> None:
    events: list[str] = []
    handler = AsyncMock()

    async def handle(message):
        events.append("committed")
        return JobDisposition.ACK

    handler.handle.side_effect = handle
    message = RcaJobMessage(
        schemaVersion=1,
        workerJobId=UUID("11111111-1111-1111-1111-111111111111"),
        rcaRunId=UUID("22222222-2222-2222-2222-222222222222"),
        incidentId=UUID("33333333-3333-3333-3333-333333333333"),
        attempt=1,
    )
    delivery = PubSubDelivery(
        data=message.to_bytes(),
        ack=lambda: events.append("ack"),
        nack=lambda: events.append("nack"),
    )
    await settle_delivery(delivery, handler)
    assert events == ["committed", "ack"]


@pytest.mark.asyncio
async def test_invalid_message_is_permanently_acked_without_handler() -> None:
    events: list[str] = []
    handler = AsyncMock()
    delivery = PubSubDelivery(
        data=b'{"endpoint":"https://evil.test"}',
        ack=lambda: events.append("ack"),
        nack=lambda: events.append("nack"),
    )
    await settle_delivery(delivery, handler)
    handler.handle.assert_not_awaited()
    assert events == ["ack"]
