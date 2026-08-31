from __future__ import annotations

from google.api_core import exceptions
from google.api_core.retry import Retry

from sre_agent.integrations.pubsub.publisher import GooglePubSubPublisher


class _RecordingFuture:
    def __init__(self) -> None:
        self.result_timeout: float | None = None

    def result(self, timeout: float | None = None) -> str:
        self.result_timeout = timeout
        return "message-id"


class _RecordingClient:
    def __init__(self) -> None:
        self.future = _RecordingFuture()
        self.retry: Retry | None = None
        self.rpc_timeout: float | None = None
        self.attributes: dict[str, str] = {}

    def publish(
        self,
        topic: str,
        data: bytes,
        *,
        retry: Retry,
        timeout: float,
        **attributes: str,
    ) -> _RecordingFuture:
        del topic, data
        self.retry = retry
        self.rpc_timeout = timeout
        self.attributes = attributes
        return self.future


def test_google_publisher_bounds_retry_rpc_and_result_wait() -> None:
    client = _RecordingClient()
    publisher = GooglePubSubPublisher(client)

    result = publisher.publish(
        "projects/local/topics/rca-jobs",
        b"payload",
        {"idempotencyKey": "rca-run:one"},
    )

    assert result == "message-id"
    assert isinstance(client.retry, Retry)
    assert client.retry.timeout == 10.0
    assert client.rpc_timeout == 10.0
    assert client.future.result_timeout == 15.0
    assert client.attributes == {"idempotencyKey": "rca-run:one"}


def test_google_publisher_preserves_pubsub_retry_policy_with_bounded_timeout() -> None:
    client = _RecordingClient()
    GooglePubSubPublisher(client).publish(
        "projects/local/topics/rca-jobs",
        b"payload",
        {"idempotencyKey": "rca-run:one"},
    )

    assert client.retry is not None
    retry_state = vars(client.retry)
    predicate = retry_state["_predicate"]
    for error_type in (
        exceptions.Aborted,
        exceptions.Cancelled,
        exceptions.DeadlineExceeded,
        exceptions.InternalServerError,
        exceptions.ResourceExhausted,
        exceptions.ServiceUnavailable,
        exceptions.Unknown,
    ):
        assert predicate(error_type("retryable")) is True
    assert predicate(exceptions.TooManyRequests("not retryable")) is False
    assert retry_state["_initial"] == 0.1
    assert retry_state["_multiplier"] == 4.0
    assert retry_state["_maximum"] == 60.0
    assert client.retry.timeout == 10.0
