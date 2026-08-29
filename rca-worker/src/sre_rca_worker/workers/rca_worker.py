from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable
from typing import NoReturn

import grpc
from google.auth.credentials import AnonymousCredentials
from google.cloud import pubsub_v1  # pyright: ignore[reportAttributeAccessIssue]
from google.pubsub_v1.services.publisher.transports.grpc import (  # pyright: ignore[reportMissingImports]
    PublisherGrpcTransport,
)
from google.pubsub_v1.services.subscriber.transports.grpc import (  # pyright: ignore[reportMissingImports]
    SubscriberGrpcTransport,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sre_rca_worker.application.rca.job_lifecycle import (
    JobDisposition,
    RcaJobHandler,
)
from sre_rca_worker.application.rca.processor import ProductionRcaProcessor
from sre_rca_worker.config.env_files import resolve_worker_env_file
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


def _load_settings() -> WorkerSettings:
    env_file = resolve_worker_env_file()
    if env_file is None:
        return WorkerSettings()  # pyright: ignore[reportCallIssue]
    return WorkerSettings(_env_file=env_file)  # pyright: ignore[reportCallIssue]


def _create_pubsub_clients(
    settings: WorkerSettings,
) -> tuple[pubsub_v1.PublisherClient, pubsub_v1.SubscriberClient]:
    if settings.pubsub_emulator_host:
        publisher_channel = grpc.insecure_channel(settings.pubsub_emulator_host)
        try:
            publisher_transport = PublisherGrpcTransport(
                channel=publisher_channel,
                credentials=AnonymousCredentials(),
            )
        except BaseException as error:  # noqa: BLE001 - preserve cancellation errors
            _reraise_after_closing(error, publisher_channel.close)
        try:
            publisher = pubsub_v1.PublisherClient(transport=publisher_transport)
        except BaseException as error:  # noqa: BLE001 - preserve construction error
            _reraise_after_closing(error, publisher_transport.close)

        try:
            subscriber_channel = grpc.insecure_channel(settings.pubsub_emulator_host)
        except BaseException as error:  # noqa: BLE001 - preserve construction error
            _reraise_after_closing(error, publisher_transport.close)
        try:
            subscriber_transport = SubscriberGrpcTransport(
                channel=subscriber_channel,
                credentials=AnonymousCredentials(),
            )
        except BaseException as error:  # noqa: BLE001 - preserve construction error
            _reraise_after_closing(
                error,
                subscriber_channel.close,
                publisher_transport.close,
            )
        try:
            subscriber = pubsub_v1.SubscriberClient(transport=subscriber_transport)
        except BaseException as error:  # noqa: BLE001 - preserve construction error
            _reraise_after_closing(
                error,
                subscriber_transport.close,
                publisher_transport.close,
            )
        return publisher, subscriber
    publisher = pubsub_v1.PublisherClient()
    try:
        subscriber = pubsub_v1.SubscriberClient()
    except BaseException as error:  # noqa: BLE001 - preserve construction error
        _reraise_after_closing(error, publisher.transport.close)
    return publisher, subscriber


def _reraise_after_closing(
    error: BaseException,
    *closers: Callable[[], None],
) -> NoReturn:
    for close in closers:
        try:
            close()
        except BaseException:  # noqa: BLE001, S110 - preserve primary failure
            pass
    raise error


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
    settings = _load_settings()
    engine = create_async_engine(settings.database_url.get_secret_value())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    processor = ProductionRcaProcessor(sessions, settings)
    handler = RcaJobHandler(
        sessions,
        processor,
        worker_id=settings.worker_id,
        deadline_seconds=settings.rca_deadline_seconds,
    )
    publisher, subscriber = _create_pubsub_clients(settings)
    primary_error: BaseException | None = None
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
    except BaseException as error:  # noqa: BLE001 - preserve runtime cancellation
        primary_error = error

    cleanup_error: BaseException | None = None
    for close in (publisher.stop, publisher.transport.close, subscriber.close):
        try:
            close()
        except BaseException as error:  # noqa: BLE001 - preserve primary failure
            if cleanup_error is None:
                cleanup_error = error
    try:
        await engine.dispose()
    except BaseException as error:  # noqa: BLE001 - preserve primary failure
        if cleanup_error is None:
            cleanup_error = error
    if primary_error is not None:
        raise primary_error
    if cleanup_error is not None:
        raise cleanup_error
