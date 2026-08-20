import os
from uuid import uuid4

import pytest
from google.cloud import pubsub_v1

from sre_rca_worker.integrations.pubsub.bootstrap import (
    ensure_topic_and_subscription,
)


@pytest.mark.skipif(
    not os.getenv("PUBSUB_EMULATOR_HOST"),
    reason="requires the official Pub/Sub Emulator",
)
def test_official_emulator_redelivers_after_nack_and_stops_after_ack() -> None:
    project_id = os.getenv("PUBSUB_PROJECT_ID", "sre-agent-test")
    suffix = uuid4().hex
    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    try:
        topic_path, subscription_path = ensure_topic_and_subscription(
            publisher,
            subscriber,
            project_id=project_id,
            topic_id=f"rca-jobs-{suffix}",
            subscription_id=f"rca-worker-{suffix}",
        )
        publisher.publish(topic_path, b"rca-message").result(timeout=10)

        first = subscriber.pull(
            request={"subscription": subscription_path, "max_messages": 1},
            timeout=10,
        ).received_messages
        assert len(first) == 1
        assert first[0].message.data == b"rca-message"
        subscriber.modify_ack_deadline(
            request={
                "subscription": subscription_path,
                "ack_ids": [first[0].ack_id],
                "ack_deadline_seconds": 0,
            }
        )

        second = subscriber.pull(
            request={"subscription": subscription_path, "max_messages": 1},
            timeout=10,
        ).received_messages
        assert len(second) == 1
        assert second[0].message.data == b"rca-message"
        subscriber.acknowledge(
            request={
                "subscription": subscription_path,
                "ack_ids": [second[0].ack_id],
            }
        )
        empty = subscriber.pull(
            request={"subscription": subscription_path, "max_messages": 1},
            timeout=1,
        ).received_messages
        assert empty == []
    finally:
        publisher.stop()
        subscriber.close()
