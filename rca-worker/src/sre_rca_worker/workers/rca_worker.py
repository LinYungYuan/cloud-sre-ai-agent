from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from sre_rca_worker.application.rca.job_lifecycle import (
    JobDisposition,
    RcaJobHandler,
)
from sre_rca_worker.integrations.pubsub.messages import RcaJobMessage
from sre_rca_worker.integrations.pubsub.subscriber import PubSubDelivery


async def settle_delivery(
    delivery: PubSubDelivery,
    handler: RcaJobHandler,
) -> None:
    try:
        message = RcaJobMessage.from_bytes(delivery.data)
    except (ValueError, ValidationError):
        delivery.ack()
        return
    disposition = await handler.handle(message)
    if disposition is JobDisposition.ACK:
        delivery.ack()
    else:
        delivery.nack()


def main(
    run: Callable[[], Awaitable[None]] | None = None,
) -> int:
    """Run an injected, fully composed Worker service without leaking errors."""
    if run is None:
        return 1

    async def drive() -> None:
        await run()

    try:
        asyncio.run(drive())
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    except Exception:  # noqa: BLE001 - CLI boundary emits only a safe exit status
        return 1
    return 0
