from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable

from google.cloud import pubsub_v1
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sre_rca_worker.application.rca.job_lifecycle import (
    JobDisposition,
    RcaJobHandler,
)
from sre_rca_worker.application.rca.processor import ProductionRcaProcessor
from sre_rca_worker.config.settings import WorkerSettings
from sre_rca_worker.integrations.pubsub.bootstrap import prepare_topic_and_subscription
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
    selected_run = run or run_production

    async def drive() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        signal_handler_installed = False
        try:
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)
            signal_handler_installed = True
        except (NotImplementedError, RuntimeError):
            pass
        try:
            if run is None:
                await run_production(stop_event)
            else:
                await selected_run()
        finally:
            if signal_handler_installed:
                loop.remove_signal_handler(signal.SIGTERM)

    try:
        asyncio.run(drive())
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    except Exception:  # noqa: BLE001 - CLI boundary emits only a safe exit status
        return 1
    return 0


async def run_production(stop_event: asyncio.Event | None = None) -> None:
    shutdown = stop_event or asyncio.Event()
    settings = WorkerSettings()  # pyright: ignore[reportCallIssue]
    engine = create_async_engine(settings.database_url.get_secret_value())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    processor = ProductionRcaProcessor(sessions, settings)
    handler = RcaJobHandler(
        sessions,
        processor,
        worker_id=settings.worker_id,
        deadline_seconds=settings.rca_deadline_seconds,
    )
    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    try:
        _, subscription = await asyncio.to_thread(
            prepare_topic_and_subscription,
            publisher,
            subscriber,
            project_id=settings.pubsub_project_id,
            topic_id=settings.rca_topic_id,
            subscription_id=settings.pubsub_subscription_id,
            auto_create=settings.pubsub_auto_create,
        )
        while not shutdown.is_set():
            response = await asyncio.to_thread(
                subscriber.pull,
                request={"subscription": subscription, "max_messages": 1},
                timeout=30,
            )
            if shutdown.is_set():
                if response.received_messages:
                    received = response.received_messages[0]
                    await asyncio.to_thread(
                        subscriber.modify_ack_deadline,
                        request={
                            "subscription": subscription,
                            "ack_ids": [received.ack_id],
                            "ack_deadline_seconds": 0,
                        },
                    )
                break
            if not response.received_messages:
                continue
            received = response.received_messages[0]
            try:
                message = RcaJobMessage.from_bytes(received.message.data)
            except (ValueError, ValidationError):
                disposition = JobDisposition.ACK
            else:
                disposition = await handler.handle(message)
            if disposition is JobDisposition.ACK:
                await asyncio.to_thread(
                    subscriber.acknowledge,
                    request={
                        "subscription": subscription,
                        "ack_ids": [received.ack_id],
                    },
                )
            else:
                await asyncio.to_thread(
                    subscriber.modify_ack_deadline,
                    request={
                        "subscription": subscription,
                        "ack_ids": [received.ack_id],
                        "ack_deadline_seconds": 0,
                    },
                )
    finally:
        publisher.stop()
        subscriber.close()
        await engine.dispose()
