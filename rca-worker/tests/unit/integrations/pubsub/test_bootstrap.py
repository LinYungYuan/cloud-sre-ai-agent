from google.api_core.exceptions import AlreadyExists

from sre_rca_worker.integrations.pubsub import bootstrap
from sre_rca_worker.integrations.pubsub.subscriber import adapt_message


class FakePublisher:
    def __init__(self, *, exists: bool = False) -> None:
        self.exists = exists
        self.topic_path_calls = []
        self.requests = []

    def topic_path(self, project_id, topic_id):
        self.topic_path_calls.append((project_id, topic_id))
        return f"projects/{project_id}/topics/{topic_id}"

    def create_topic(self, *, request):
        self.requests.append(request)
        if self.exists:
            raise AlreadyExists("exists")


class FakeSubscriber:
    def __init__(self, *, exists: bool = False) -> None:
        self.exists = exists
        self.subscription_path_calls = []
        self.requests = []

    def subscription_path(self, project_id, subscription_id):
        self.subscription_path_calls.append((project_id, subscription_id))
        return f"projects/{project_id}/subscriptions/{subscription_id}"

    def create_subscription(self, *, request):
        self.requests.append(request)
        if self.exists:
            raise AlreadyExists("exists")


def test_prepare_without_auto_create_only_resolves_existing_resource_paths() -> None:
    publisher = FakePublisher()
    subscriber = FakeSubscriber()

    topic, subscription = bootstrap.prepare_topic_and_subscription(
        publisher,
        subscriber,
        project_id="project",
        topic_id="topic",
        subscription_id="subscription",
        auto_create=False,
    )

    assert topic == "projects/project/topics/topic"
    assert subscription == "projects/project/subscriptions/subscription"
    assert publisher.topic_path_calls == [("project", "topic")]
    assert subscriber.subscription_path_calls == [("project", "subscription")]
    assert publisher.requests == []
    assert subscriber.requests == []


def test_prepare_with_auto_create_is_idempotent_for_existing_resources() -> None:
    topic, subscription = bootstrap.prepare_topic_and_subscription(
        FakePublisher(exists=True),
        FakeSubscriber(exists=True),
        project_id="project",
        topic_id="topic",
        subscription_id="subscription",
        auto_create=True,
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
