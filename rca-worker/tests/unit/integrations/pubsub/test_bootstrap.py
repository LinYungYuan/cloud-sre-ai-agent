from google.api_core.exceptions import AlreadyExists

from sre_rca_worker.integrations.pubsub.bootstrap import (
    ensure_topic_and_subscription,
)
from sre_rca_worker.integrations.pubsub.subscriber import adapt_message


class FakePublisher:
    def __init__(self, *, exists: bool = False) -> None:
        self.exists = exists
        self.requests = []

    def topic_path(self, project_id, topic_id):
        return f"projects/{project_id}/topics/{topic_id}"

    def create_topic(self, *, request):
        self.requests.append(request)
        if self.exists:
            raise AlreadyExists("exists")


class FakeSubscriber:
    def __init__(self, *, exists: bool = False) -> None:
        self.exists = exists
        self.requests = []

    def subscription_path(self, project_id, subscription_id):
        return f"projects/{project_id}/subscriptions/{subscription_id}"

    def create_subscription(self, *, request):
        self.requests.append(request)
        if self.exists:
            raise AlreadyExists("exists")


def test_bootstrap_is_idempotent_for_existing_topic_and_subscription() -> None:
    topic, subscription = ensure_topic_and_subscription(
        FakePublisher(exists=True),
        FakeSubscriber(exists=True),
        project_id="project",
        topic_id="topic",
        subscription_id="subscription",
    )

    assert topic == "projects/project/topics/topic"
    assert subscription == "projects/project/subscriptions/subscription"


def test_delivery_exposes_only_data_ack_and_nack() -> None:
    calls = []

    class Message:
        data = b"payload"

        @staticmethod
        def ack():
            calls.append("ack")

        @staticmethod
        def nack():
            calls.append("nack")

    delivery = adapt_message(Message())
    delivery.nack()
    delivery.ack()

    assert delivery.data == b"payload"
    assert calls == ["nack", "ack"]
