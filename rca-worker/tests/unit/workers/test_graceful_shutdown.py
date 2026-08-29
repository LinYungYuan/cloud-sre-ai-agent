from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.auth.credentials import AnonymousCredentials

from sre_rca_worker.application.rca.job_lifecycle import JobDisposition
from sre_rca_worker.config.settings import WorkerSettings
from sre_rca_worker.integrations.pubsub.messages import RcaJobMessage
from sre_rca_worker.workers import rca_worker


def _worker_settings(
    *,
    app_environment: str,
    pubsub_emulator_host: str | None,
) -> WorkerSettings:
    return WorkerSettings.model_validate(
        {
            "database_url": "postgresql+asyncpg://app@db/sre",
            "pubsub_project_id": "project",
            "rca_topic_id": "topic",
            "pubsub_subscription_id": "subscription",
            "app_environment": app_environment,
            "model_name": "test-model",
            "pubsub_emulator_host": pubsub_emulator_host,
        }
    )


def test_pubsub_clients_use_independent_explicit_insecure_emulator_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _worker_settings(
        app_environment="local",
        pubsub_emulator_host="127.0.0.1:58085",
    )
    channels = [object(), object()]
    created_channels: list[str] = []
    configured_transports: list[tuple[object, object, AnonymousCredentials]] = []
    configured_clients: list[object] = []

    class FakePublisherTransport:
        def __init__(
            self,
            *,
            channel: object,
            credentials: AnonymousCredentials,
        ) -> None:
            configured_transports.append((self, channel, credentials))

    class FakeSubscriberTransport(FakePublisherTransport):
        pass

    class FakePublisher:
        def __init__(self, *, transport: object) -> None:
            configured_clients.append(transport)

    class FakeSubscriber(FakePublisher):
        pass

    monkeypatch.setenv("PUBSUB_EMULATOR_HOST", "external-value-must-not-change")
    monkeypatch.setattr(
        rca_worker.grpc,
        "insecure_channel",
        lambda host: (created_channels.append(host), channels.pop(0))[1],
    )
    monkeypatch.setattr(
        rca_worker,
        "PublisherGrpcTransport",
        FakePublisherTransport,
    )
    monkeypatch.setattr(
        rca_worker,
        "SubscriberGrpcTransport",
        FakeSubscriberTransport,
    )
    monkeypatch.setattr(rca_worker.pubsub_v1, "PublisherClient", FakePublisher)
    monkeypatch.setattr(rca_worker.pubsub_v1, "SubscriberClient", FakeSubscriber)

    publisher, subscriber = rca_worker._create_pubsub_clients(settings)

    assert isinstance(publisher, FakePublisher)
    assert isinstance(subscriber, FakeSubscriber)
    assert created_channels == [
        "127.0.0.1:58085",
        "127.0.0.1:58085",
    ]
    assert configured_transports[0][1] is not configured_transports[1][1]
    assert all(
        isinstance(credentials, AnonymousCredentials)
        for _, _, credentials in configured_transports
    )
    assert configured_clients == [
        configured_transports[0][0],
        configured_transports[1][0],
    ]
    assert os.environ["PUBSUB_EMULATOR_HOST"] == "external-value-must-not-change"


def test_pubsub_clients_use_adc_defaults_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _worker_settings(
        app_environment="production",
        pubsub_emulator_host=None,
    )
    constructor_arguments: list[dict[str, object]] = []

    class FakePublisher:
        class Transport:
            def close(self) -> None:
                return None

        def __init__(self, **kwargs: object) -> None:
            constructor_arguments.append(kwargs)
            self.transport = self.Transport()

    class FakeSubscriber(FakePublisher):
        pass

    monkeypatch.setattr(rca_worker.pubsub_v1, "PublisherClient", FakePublisher)
    monkeypatch.setattr(rca_worker.pubsub_v1, "SubscriberClient", FakeSubscriber)

    publisher, subscriber = rca_worker._create_pubsub_clients(settings)

    assert isinstance(publisher, FakePublisher)
    assert isinstance(subscriber, FakeSubscriber)
    assert constructor_arguments == [{}, {}]


def test_pubsub_clients_close_publisher_transport_when_adc_subscriber_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _worker_settings(
        app_environment="production",
        pubsub_emulator_host=None,
    )
    events: list[str] = []

    class FakePublisher:
        class Transport:
            def close(self) -> None:
                events.append("publisher transport.close")

        def __init__(self) -> None:
            self.transport = self.Transport()

    class FailingSubscriber:
        def __init__(self) -> None:
            raise RuntimeError("adc subscriber construction failed")

    monkeypatch.setattr(rca_worker.pubsub_v1, "PublisherClient", FakePublisher)
    monkeypatch.setattr(
        rca_worker.pubsub_v1,
        "SubscriberClient",
        FailingSubscriber,
    )

    with pytest.raises(RuntimeError, match="adc subscriber construction failed"):
        rca_worker._create_pubsub_clients(settings)

    assert events == ["publisher transport.close"]


def test_pubsub_clients_close_publisher_transport_when_second_channel_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _worker_settings(
        app_environment="local",
        pubsub_emulator_host="127.0.0.1:58085",
    )
    events: list[str] = []
    channels = [object()]

    class FakePublisherTransport:
        def __init__(self, **_: object) -> None:
            pass

        def close(self) -> None:
            events.append("publisher transport.close")
            raise RuntimeError("publisher cleanup failed")

    class FakePublisher:
        def __init__(self, **_: object) -> None:
            pass

    def create_channel(_: str) -> object:
        if channels:
            return channels.pop()
        raise RuntimeError("subscriber channel failed")

    monkeypatch.setattr(rca_worker.grpc, "insecure_channel", create_channel)
    monkeypatch.setattr(
        rca_worker,
        "PublisherGrpcTransport",
        FakePublisherTransport,
    )
    monkeypatch.setattr(rca_worker.pubsub_v1, "PublisherClient", FakePublisher)

    with pytest.raises(RuntimeError, match="subscriber channel failed"):
        rca_worker._create_pubsub_clients(settings)

    assert events == ["publisher transport.close"]


def test_pubsub_clients_close_created_transports_when_subscriber_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _worker_settings(
        app_environment="local",
        pubsub_emulator_host="127.0.0.1:58085",
    )
    events: list[str] = []

    class FakePublisherTransport:
        def __init__(self, **_: object) -> None:
            pass

        def close(self) -> None:
            events.append("publisher transport.close")
            raise RuntimeError("publisher transport cleanup failed")

    class FakeSubscriberTransport:
        def __init__(self, **_: object) -> None:
            pass

        def close(self) -> None:
            events.append("subscriber transport.close")
            raise RuntimeError("subscriber transport cleanup failed")

    class FakePublisher:
        def __init__(self, **_: object) -> None:
            pass

    class FailingSubscriber:
        def __init__(self, **_: object) -> None:
            raise RuntimeError("subscriber construction failed")

    monkeypatch.setattr(rca_worker.grpc, "insecure_channel", lambda _: object())
    monkeypatch.setattr(
        rca_worker,
        "PublisherGrpcTransport",
        FakePublisherTransport,
    )
    monkeypatch.setattr(
        rca_worker,
        "SubscriberGrpcTransport",
        FakeSubscriberTransport,
    )
    monkeypatch.setattr(rca_worker.pubsub_v1, "PublisherClient", FakePublisher)
    monkeypatch.setattr(
        rca_worker.pubsub_v1,
        "SubscriberClient",
        FailingSubscriber,
    )

    with pytest.raises(RuntimeError, match="subscriber construction failed"):
        rca_worker._create_pubsub_clients(settings)

    assert events == ["subscriber transport.close", "publisher transport.close"]


def test_sigterm_waits_for_inflight_coroutine_before_clean_exit(
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    script = """
import asyncio
import os
from pathlib import Path

from sre_rca_worker.workers.rca_worker import main

async def run():
    Path(os.environ["STARTED_PATH"]).write_text("started", encoding="utf-8")
    await asyncio.sleep(0.75)
    Path(os.environ["FINISHED_PATH"]).write_text("finished", encoding="utf-8")

raise SystemExit(main(run))
"""
    environment = {
        **os.environ,
        "STARTED_PATH": str(started),
        "FINISHED_PATH": str(finished),
    }
    process = subprocess.Popen([sys.executable, "-c", script], env=environment)
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and process.poll() is None:
            assert time.monotonic() < deadline, "worker did not enter coroutine"
            time.sleep(0.01)

        assert started.exists(), f"worker exited early with status {process.returncode}"
        process.send_signal(signal.SIGTERM)
        time.sleep(0.1)
        assert process.poll() is None, "SIGTERM cancelled the in-flight coroutine"
        assert process.wait(timeout=5) == 0
        assert finished.read_text(encoding="utf-8") == "finished"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_stop_failure_still_closes_worker_pubsub_transports_and_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    events: list[str] = []
    settings = WorkerSettings.model_validate(
        {
            "database_url": "postgresql+asyncpg://app@db/sre",
            "pubsub_project_id": "project",
            "rca_topic_id": "topic",
            "pubsub_subscription_id": "subscription",
            "app_environment": "local",
            "model_name": "test-model",
            "worker_id": "worker",
        }
    )

    class FakeEngine:
        async def dispose(self) -> None:
            events.append("engine.dispose")
            raise RuntimeError("engine cleanup failed")

    class FakePublisher:
        class Transport:
            def close(self) -> None:
                events.append("publisher transport.close")
                raise RuntimeError("publisher transport cleanup failed")

        def __init__(self) -> None:
            self.transport = self.Transport()

        def stop(self) -> None:
            events.append("publisher.stop")
            raise RuntimeError("publisher stop failed")

    class FakeSubscriber:
        def close(self) -> None:
            events.append("subscriber.close")
            raise RuntimeError("subscriber cleanup failed")

    monkeypatch.setattr(rca_worker, "WorkerSettings", lambda: settings)
    monkeypatch.setattr(rca_worker, "create_async_engine", lambda _: FakeEngine())
    monkeypatch.setattr(
        rca_worker, "async_sessionmaker", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(rca_worker, "ProductionRcaProcessor", lambda *args: object())
    monkeypatch.setattr(rca_worker, "RcaJobHandler", lambda *args, **kwargs: object())
    monkeypatch.setattr(rca_worker.pubsub_v1, "PublisherClient", FakePublisher)
    monkeypatch.setattr(rca_worker.pubsub_v1, "SubscriberClient", FakeSubscriber)
    monkeypatch.setattr(
        rca_worker,
        "prepare_topic_and_subscription",
        lambda *args, **kwargs: ("topic", "subscription-path"),
    )

    with pytest.raises(RuntimeError, match="publisher stop failed"):
        await rca_worker.run_production(stop_event)

    assert events == [
        "publisher.stop",
        "publisher transport.close",
        "subscriber.close",
        "engine.dispose",
    ]


@pytest.mark.asyncio
async def test_stop_during_pull_releases_received_message_without_starting_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = asyncio.Event()
    message = RcaJobMessage.model_validate(
        {
            "schemaVersion": 1,
            "workerJobId": "11111111-1111-1111-1111-111111111111",
            "rcaRunId": "22222222-2222-2222-2222-222222222222",
            "incidentId": "33333333-3333-3333-3333-333333333333",
            "attempt": 1,
        }
    )
    requests: list[dict[str, object]] = []
    cleanup_events: list[str] = []
    settings = WorkerSettings.model_validate(
        {
            "database_url": "postgresql+asyncpg://app@db/sre",
            "pubsub_project_id": "project",
            "rca_topic_id": "topic",
            "pubsub_subscription_id": "subscription",
            "app_environment": "local",
            "model_name": "test-model",
            "worker_id": "worker",
        }
    )

    class FakeEngine:
        async def dispose(self) -> None:
            cleanup_events.append("engine.dispose")

    class FakePublisher:
        class Transport:
            def close(self) -> None:
                cleanup_events.append("publisher transport.close")

        def __init__(self) -> None:
            self.transport = self.Transport()

        def stop(self) -> None:
            cleanup_events.append("publisher.stop")

    class FakeSubscriber:
        def pull(self, *, request: dict[str, object], timeout: int):
            stop_event.set()
            return SimpleNamespace(
                received_messages=[
                    SimpleNamespace(
                        message=SimpleNamespace(data=message.to_bytes()),
                        ack_id="ack-1",
                    )
                ]
            )

        def acknowledge(self, *, request: dict[str, object]) -> None:
            raise AssertionError(
                "a message not handed to the handler must not be acked"
            )

        def modify_ack_deadline(self, *, request: dict[str, object]) -> None:
            requests.append(request)

        def close(self) -> None:
            cleanup_events.append("subscriber.close")

    class RejectingHandler:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def handle(self, message: RcaJobMessage):
            raise AssertionError("shutdown must not start a newly pulled job")

    monkeypatch.setattr(rca_worker, "WorkerSettings", lambda: settings)
    monkeypatch.setattr(rca_worker, "create_async_engine", lambda _: FakeEngine())
    monkeypatch.setattr(
        rca_worker, "async_sessionmaker", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(rca_worker, "ProductionRcaProcessor", lambda *args: object())
    monkeypatch.setattr(rca_worker, "RcaJobHandler", RejectingHandler)
    monkeypatch.setattr(rca_worker.pubsub_v1, "PublisherClient", FakePublisher)
    monkeypatch.setattr(rca_worker.pubsub_v1, "SubscriberClient", FakeSubscriber)
    monkeypatch.setattr(
        rca_worker,
        "prepare_topic_and_subscription",
        lambda *args, **kwargs: ("topic", "subscription-path"),
    )

    await rca_worker.run_production(stop_event)

    assert requests == [
        {
            "subscription": "subscription-path",
            "ack_ids": ["ack-1"],
            "ack_deadline_seconds": 0,
        }
    ]
    assert cleanup_events == [
        "publisher.stop",
        "publisher transport.close",
        "subscriber.close",
        "engine.dispose",
    ]


@pytest.mark.asyncio
async def test_stop_during_handler_finishes_ack_then_exits_without_another_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = asyncio.Event()
    message = RcaJobMessage.model_validate(
        {
            "schemaVersion": 1,
            "workerJobId": "11111111-1111-1111-1111-111111111111",
            "rcaRunId": "22222222-2222-2222-2222-222222222222",
            "incidentId": "33333333-3333-3333-3333-333333333333",
            "attempt": 1,
        }
    )
    events: list[str] = []
    settings = WorkerSettings.model_validate(
        {
            "database_url": "postgresql+asyncpg://app@db/sre",
            "pubsub_project_id": "project",
            "rca_topic_id": "topic",
            "pubsub_subscription_id": "subscription",
            "app_environment": "local",
            "model_name": "test-model",
            "worker_id": "worker",
        }
    )

    class FakeEngine:
        async def dispose(self) -> None:
            return None

    class FakePublisher:
        class Transport:
            def close(self) -> None:
                return None

        def __init__(self) -> None:
            self.transport = self.Transport()

        def stop(self) -> None:
            return None

    class FakeSubscriber:
        def __init__(self) -> None:
            self.pull_count = 0

        def pull(self, *, request: dict[str, object], timeout: int):
            self.pull_count += 1
            if self.pull_count > 1:
                raise AssertionError("shutdown must prevent another pull")
            return SimpleNamespace(
                received_messages=[
                    SimpleNamespace(
                        message=SimpleNamespace(data=message.to_bytes()),
                        ack_id="ack-1",
                    )
                ]
            )

        def acknowledge(self, *, request: dict[str, object]) -> None:
            events.append(f"ack:{request['ack_ids']}")

        def modify_ack_deadline(self, *, request: dict[str, object]) -> None:
            raise AssertionError("a completed handler must use its disposition")

        def close(self) -> None:
            return None

    class CompletingHandler:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def handle(self, message: RcaJobMessage) -> JobDisposition:
            events.append("handler-started")
            stop_event.set()
            await asyncio.sleep(0.05)
            events.append("handler-finished")
            return JobDisposition.ACK

    monkeypatch.setattr(rca_worker, "WorkerSettings", lambda: settings)
    monkeypatch.setattr(rca_worker, "create_async_engine", lambda _: FakeEngine())
    monkeypatch.setattr(
        rca_worker, "async_sessionmaker", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(rca_worker, "ProductionRcaProcessor", lambda *args: object())
    monkeypatch.setattr(rca_worker, "RcaJobHandler", CompletingHandler)
    monkeypatch.setattr(rca_worker.pubsub_v1, "PublisherClient", FakePublisher)
    monkeypatch.setattr(rca_worker.pubsub_v1, "SubscriberClient", FakeSubscriber)
    monkeypatch.setattr(
        rca_worker,
        "prepare_topic_and_subscription",
        lambda *args, **kwargs: ("topic", "subscription-path"),
    )

    await rca_worker.run_production(stop_event)

    assert events == ["handler-started", "handler-finished", "ack:['ack-1']"]
