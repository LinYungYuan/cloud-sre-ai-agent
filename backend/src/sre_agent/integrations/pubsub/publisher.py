from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Protocol

from google.api_core.client_options import ClientOptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import pubsub_v1


class MessagePublisher(Protocol):
    def publish(
        self, topic: str, data: bytes, attributes: Mapping[str, str]
    ) -> str: ...


class PublishFuture(Protocol):
    def result(self) -> str: ...


class PubSubClient(Protocol):
    def publish(
        self, topic: str, data: bytes, **attributes: str
    ) -> PublishFuture: ...


def create_publisher_client(
    pubsub_emulator_host: str | None,
) -> pubsub_v1.PublisherClient:
    """Create an explicit emulator client without mutating process environment."""
    if pubsub_emulator_host is None:
        return pubsub_v1.PublisherClient()
    return pubsub_v1.PublisherClient(
        credentials=AnonymousCredentials(),
        client_options=ClientOptions(api_endpoint=pubsub_emulator_host),
    )


class GooglePubSubPublisher:
    """Pub/Sub boundary with an injected client and synchronous acknowledgement."""

    def __init__(self, client: PubSubClient) -> None:
        self._client = client

    def publish(
        self, topic: str, data: bytes, attributes: Mapping[str, str]
    ) -> str:
        return self._client.publish(topic, data, **dict(attributes)).result()


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
