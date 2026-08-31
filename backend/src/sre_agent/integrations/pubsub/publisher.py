from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import NoReturn, Protocol

import grpc
from google.api_core import exceptions
from google.api_core.retry import Retry, if_exception_type
from google.auth.credentials import AnonymousCredentials
from google.cloud import pubsub_v1  # pyright: ignore[reportAttributeAccessIssue]
from google.pubsub_v1.services.publisher.transports.grpc import (  # pyright: ignore[reportMissingImports]
    PublisherGrpcTransport,
)


class MessagePublisher(Protocol):
    def publish(
        self, topic: str, data: bytes, attributes: Mapping[str, str]
    ) -> str: ...


class PublishFuture(Protocol):
    def result(self, timeout: float | None = None) -> str: ...


class PubSubClient(Protocol):
    def publish(
        self,
        topic: str,
        data: bytes,
        *,
        retry: Retry,
        timeout: float,
        **attributes: str,
    ) -> PublishFuture: ...


_PUBLISH_RETRY_TIMEOUT_SECONDS = 10.0
_PUBLISH_RPC_TIMEOUT_SECONDS = 10.0
_PUBLISH_RESULT_TIMEOUT_SECONDS = 15.0
_PUBLISH_RETRY = Retry(
    predicate=if_exception_type(
        exceptions.Aborted,
        exceptions.Cancelled,
        exceptions.DeadlineExceeded,
        exceptions.InternalServerError,
        exceptions.ResourceExhausted,
        exceptions.ServiceUnavailable,
        exceptions.Unknown,
    ),
    initial=0.1,
    multiplier=4.0,
    maximum=60.0,
    timeout=_PUBLISH_RETRY_TIMEOUT_SECONDS,
)


def create_publisher_client(
    pubsub_emulator_host: str | None,
) -> pubsub_v1.PublisherClient:
    """Create an explicit emulator client without mutating process environment."""
    if pubsub_emulator_host is None:
        return pubsub_v1.PublisherClient()
    channel = grpc.insecure_channel(pubsub_emulator_host)
    try:
        transport = PublisherGrpcTransport(
            channel=channel,
            credentials=AnonymousCredentials(),
        )
    except BaseException as error:  # noqa: BLE001 - preserve construction error
        _reraise_after_closing(error, channel.close)
    try:
        return pubsub_v1.PublisherClient(transport=transport)
    except BaseException as error:  # noqa: BLE001 - preserve construction error
        _reraise_after_closing(error, transport.close)


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


def close_publisher_transport(client: pubsub_v1.PublisherClient) -> None:
    """Close the gRPC transport after the publisher has stopped batching."""
    client.transport.close()


class GooglePubSubPublisher:
    """Pub/Sub boundary with an injected client and synchronous acknowledgement."""

    def __init__(self, client: PubSubClient) -> None:
        self._client = client

    def publish(
        self, topic: str, data: bytes, attributes: Mapping[str, str]
    ) -> str:
        future = self._client.publish(
            topic,
            data,
            retry=_PUBLISH_RETRY,
            timeout=_PUBLISH_RPC_TIMEOUT_SECONDS,
            **dict(attributes),
        )
        return future.result(timeout=_PUBLISH_RESULT_TIMEOUT_SECONDS)


class FakeMessagePublisher:
    """In-memory publisher for callers that need a deterministic test double."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.messages: list[tuple[str, bytes, dict[str, str]]] = []

    def publish(
        self, topic: str, data: bytes, attributes: Mapping[str, str]
    ) -> str:
        with self._lock:
            self.messages.append((topic, data, dict(attributes)))
            return f"fake-message-{len(self.messages)}"
