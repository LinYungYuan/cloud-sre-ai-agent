from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PubSubDelivery:
    data: bytes
    ack: Callable[[], None]
    nack: Callable[[], None]


def adapt_message(message) -> PubSubDelivery:
    return PubSubDelivery(data=message.data, ack=message.ack, nack=message.nack)
