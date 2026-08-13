from __future__ import annotations

import asyncio
from collections.abc import Callable

from google.cloud import pubsub_v1
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sre_agent.config.settings import Settings
from sre_agent.integrations.pubsub.publisher import GooglePubSubPublisher
from sre_agent.workers.outbox_worker import OutboxPublisher


async def run_outbox_worker(*, poll_seconds: float = 1.0) -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue]
    engine = create_async_engine(settings.database_url.get_secret_value())
    client = pubsub_v1.PublisherClient()
    topic = client.topic_path(settings.pubsub_project_id, settings.rca_topic_id)
    worker = OutboxPublisher(
        async_sessionmaker(engine, expire_on_commit=False),
        GooglePubSubPublisher(client),
        topic,
    )
    try:
        while True:
            published = await worker.publish_batch(limit=100)
            if published == 0:
                await asyncio.sleep(poll_seconds)
    finally:
        client.stop()
        await engine.dispose()


def _run() -> None:
    asyncio.run(run_outbox_worker())


def main(*, run: Callable[[], None] = _run) -> int:
    try:
        run()
    except (KeyboardInterrupt, SystemExit):
        return 0
    except Exception:  # noqa: BLE001 -- process boundary must fail without leaking config
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
