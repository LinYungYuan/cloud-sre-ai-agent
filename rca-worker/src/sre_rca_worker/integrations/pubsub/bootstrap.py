from google.api_core.exceptions import AlreadyExists


def ensure_topic_and_subscription(
    publisher,
    subscriber,
    *,
    project_id: str,
    topic_id: str,
    subscription_id: str,
) -> tuple[str, str]:
    topic_path = publisher.topic_path(project_id, topic_id)
    subscription_path = subscriber.subscription_path(project_id, subscription_id)
    try:
        publisher.create_topic(request={"name": topic_path})
    except AlreadyExists:
        pass
    try:
        subscriber.create_subscription(
            request={"name": subscription_path, "topic": topic_path}
        )
    except AlreadyExists:
        pass
    return topic_path, subscription_path
