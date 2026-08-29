import os
from pathlib import Path
from time import perf_counter
from typing import Self
from uuid import UUID

import httpx
import pytest
from google.auth.credentials import AnonymousCredentials
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sre_agent.api import composition
from sre_agent.api.main import create_app
from sre_agent.config.settings import Settings
from sre_agent.integrations.pubsub import publisher as pubsub_publisher
from sre_agent.integrations.pubsub.messages import RcaJobMessage

SOURCE_ID = UUID("58000000-0000-0000-0000-000000000001")
TEAM_ID = UUID("18000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("28000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("38000000-0000-0000-0000-000000000001")
SERVICE_ID = UUID("48000000-0000-0000-0000-000000000001")
DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
).replace("postgresql://", "postgresql+asyncpg://", 1)
EXAMPLE = (
    Path(__file__).resolve().parents[4] / "contracts/examples/grafana-firing-aws.json"
).read_bytes()


class _PublishFuture:
    def result(self) -> str:
        return "published-message"


class RecordingPublisherClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes, dict[str, str]]] = []
        self.stopped = False

    def topic_path(self, project_id: str, topic_id: str) -> str:
        return f"projects/{project_id}/topics/{topic_id}"

    def publish(self, topic: str, data: bytes, **attributes: str) -> _PublishFuture:
        self.messages.append((topic, data, attributes))
        return _PublishFuture()

    def stop(self) -> None:
        self.stopped = True


class _LifecycleConnection:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def execute(self, statement: object) -> None:
        del statement


class _LifecycleEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def connect(self) -> _LifecycleConnection:
        return _LifecycleConnection()

    async def dispose(self) -> None:
        self.dispose_calls += 1


class _LifecyclePublisherTransport:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.close_error = close_error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _LifecyclePublisherClient:
    def __init__(
        self,
        *,
        topic_error: Exception | None = None,
        stop_error: Exception | None = None,
        transport_close_error: Exception | None = None,
    ) -> None:
        self.topic_error = topic_error
        self.stop_error = stop_error
        self.stop_calls = 0
        self.transport = _LifecyclePublisherTransport(
            close_error=transport_close_error
        )

    def topic_path(self, project_id: str, topic_id: str) -> str:
        del project_id, topic_id
        if self.topic_error is not None:
            raise self.topic_error
        return "projects/test/topics/rca-jobs"

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error


def _resource_settings() -> Settings:
    return Settings(
        database_url=SecretStr("postgresql+asyncpg://unused"),
        grafana_tokens={SOURCE_ID: {"token": SecretStr("accepted-token")}},
        pubsub_project_id="local-project",
        rca_topic_id="rca-jobs",
        app_environment="local",
    )


def _stub_resource_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    engine: _LifecycleEngine,
) -> None:
    monkeypatch.setattr(composition, "create_async_engine", lambda _: engine)
    monkeypatch.setattr(
        composition,
        "async_sessionmaker",
        lambda *args, **kwargs: object(),
    )

    async def load_provider(_: object) -> object:
        return object()

    monkeypatch.setattr(composition, "load_normalization_rule_provider", load_provider)
    monkeypatch.setattr(composition, "load_folder_scope_provider", load_provider)
    monkeypatch.setattr(composition, "_validate_configured_sources", lambda *_: None)


@pytest.mark.asyncio
async def test_production_resources_disposes_engine_when_publisher_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _LifecycleEngine()
    _stub_resource_dependencies(monkeypatch, engine)

    def fail_create(_: str | None) -> _LifecyclePublisherClient:
        raise RuntimeError("publisher construction failed")

    monkeypatch.setattr(composition, "create_publisher_client", fail_create)

    with pytest.raises(RuntimeError, match="publisher construction failed"):
        async with composition.production_resources(_resource_settings()):
            raise AssertionError("resource acquisition should fail")

    assert engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_production_resources_stops_client_and_disposes_engine_when_topic_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _LifecycleEngine()
    client = _LifecyclePublisherClient(topic_error=RuntimeError("topic failed"))
    _stub_resource_dependencies(monkeypatch, engine)
    monkeypatch.setattr(composition, "create_publisher_client", lambda _: client)

    with pytest.raises(RuntimeError, match="topic failed"):
        async with composition.production_resources(_resource_settings()):
            raise AssertionError("topic acquisition should fail")

    assert client.stop_calls == 1
    assert client.transport.close_calls == 1
    assert engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_production_resources_disposes_engine_when_client_stop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _LifecycleEngine()
    client = _LifecyclePublisherClient(stop_error=RuntimeError("stop failed"))
    _stub_resource_dependencies(monkeypatch, engine)
    monkeypatch.setattr(composition, "create_publisher_client", lambda _: client)

    with pytest.raises(RuntimeError, match="stop failed"):
        async with composition.production_resources(_resource_settings()):
            pass

    assert client.stop_calls == 1
    assert client.transport.close_calls == 1
    assert engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_production_resources_stops_client_and_disposes_engine_on_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _LifecycleEngine()
    client = _LifecyclePublisherClient()
    _stub_resource_dependencies(monkeypatch, engine)
    monkeypatch.setattr(composition, "create_publisher_client", lambda _: client)

    async with composition.production_resources(_resource_settings()):
        pass

    assert client.stop_calls == 1
    assert client.transport.close_calls == 1
    assert engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_production_resources_disposes_engine_when_publisher_transport_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _LifecycleEngine()
    client = _LifecyclePublisherClient(
        transport_close_error=RuntimeError("transport close failed")
    )
    _stub_resource_dependencies(monkeypatch, engine)
    monkeypatch.setattr(composition, "create_publisher_client", lambda _: client)

    with pytest.raises(RuntimeError, match="transport close failed"):
        async with composition.production_resources(_resource_settings()):
            pass

    assert client.stop_calls == 1
    assert client.transport.close_calls == 1
    assert engine.dispose_calls == 1


def test_local_emulator_client_uses_explicit_insecure_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = object()
    constructed: dict[str, object] = {}

    class FakePublisherTransport:
        def __init__(
            self,
            *,
            channel: object,
            credentials: AnonymousCredentials,
        ) -> None:
            constructed["created_transport"] = self
            constructed["transport_channel"] = channel
            constructed["transport_credentials"] = credentials

    class FakePublisherClient:
        def __init__(self, **values: object) -> None:
            constructed.update(values)

    monkeypatch.setenv("PUBSUB_EMULATOR_HOST", "external-value-must-not-change")
    monkeypatch.setattr(
        pubsub_publisher.grpc,
        "insecure_channel",
        lambda host: constructed.setdefault("channel_host", host) and channel,
    )
    monkeypatch.setattr(
        pubsub_publisher,
        "PublisherGrpcTransport",
        FakePublisherTransport,
    )
    monkeypatch.setattr(
        pubsub_publisher.pubsub_v1, "PublisherClient", FakePublisherClient
    )

    pubsub_publisher.create_publisher_client("127.0.0.1:58085")

    assert constructed["channel_host"] == "127.0.0.1:58085"
    assert constructed["transport_channel"] is channel
    assert isinstance(constructed["transport_credentials"], AnonymousCredentials)
    assert constructed["transport"] is constructed["created_transport"]
    assert os.environ["PUBSUB_EMULATOR_HOST"] == "external-value-must-not-change"


def test_local_emulator_client_closes_transport_when_client_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeChannel:
        def close(self) -> None:
            events.append("channel.close")

    class FakePublisherTransport:
        def __init__(self, **_: object) -> None:
            pass

        def close(self) -> None:
            events.append("transport.close")

    class FailingPublisherClient:
        def __init__(self, **_: object) -> None:
            raise RuntimeError("client construction failed")

    monkeypatch.setattr(
        pubsub_publisher.grpc,
        "insecure_channel",
        lambda _: FakeChannel(),
    )
    monkeypatch.setattr(
        pubsub_publisher,
        "PublisherGrpcTransport",
        FakePublisherTransport,
    )
    monkeypatch.setattr(
        pubsub_publisher.pubsub_v1,
        "PublisherClient",
        FailingPublisherClient,
    )

    with pytest.raises(RuntimeError, match="client construction failed"):
        pubsub_publisher.create_publisher_client("127.0.0.1:58085")

    assert events == ["transport.close"]


def test_publisher_client_uses_adc_defaults_without_an_emulator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[dict[str, object]] = []

    class FakePublisherClient:
        def __init__(self, **values: object) -> None:
            constructed.append(values)

    monkeypatch.setattr(
        pubsub_publisher.pubsub_v1, "PublisherClient", FakePublisherClient
    )

    client = pubsub_publisher.create_publisher_client(None)

    assert isinstance(client, FakePublisherClient)
    assert constructed == [{}]


@pytest.mark.asyncio
async def test_production_resources_accept_and_commit_without_dependency_overrides(
    monkeypatch: pytest.MonkeyPatch,
):
    engine = create_async_engine(DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                TRUNCATE TABLE outbox_events, worker_jobs, rca_runs,
                    incident_alerts, incidents, ingestion_dedup_keys,
                    alert_instances, alert_events, webhook_deliveries,
                    grafana_sources, services, environments, projects, teams
                CASCADE
                """
            )
        )
        values = {
            "team_id": TEAM_ID,
            "project_id": PROJECT_ID,
            "environment_id": ENVIRONMENT_ID,
            "service_id": SERVICE_ID,
            "source_id": SOURCE_ID,
        }
        for statement in (
            "INSERT INTO teams (id, name) VALUES (:team_id, 'platform')",
            (
                "INSERT INTO projects (id, team_id, name) "
                "VALUES (:project_id, :team_id, 'svc-lx-afa-01-uat-1b9a87')"
            ),
            (
                "INSERT INTO environments (id, project_id, name) "
                "VALUES (:environment_id, :project_id, 'uat')"
            ),
            (
                "INSERT INTO services (id, environment_id, name) "
                "VALUES (:service_id, :environment_id, 'aaaa')"
            ),
            (
                "INSERT INTO grafana_sources (id, project_id, environment_id, name) "
                "VALUES (:source_id, :project_id, :environment_id, 'production-app')"
            ),
        ):
            await connection.execute(text(statement), values)

    settings = Settings(
        database_url=SecretStr(DATABASE_URL),
        grafana_tokens={SOURCE_ID: {"current-2026-08": SecretStr("accepted-token")}},
        pubsub_project_id="local-project",
        rca_topic_id="rca-jobs",
        pubsub_emulator_host="127.0.0.1:58085",
        app_environment="local",
    )
    readiness_statements: list[str] = []
    publisher_client = RecordingPublisherClient()

    def capture_statement(_, __, statement, ___, ____, _____) -> None:
        if statement.strip() == "SELECT 1":
            readiness_statements.append(statement.strip())

    event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)
    monkeypatch.setattr(composition, "create_async_engine", lambda _: engine)

    def build_publisher_client(host: str | None) -> RecordingPublisherClient:
        assert host == "127.0.0.1:58085"
        return publisher_client

    monkeypatch.setattr(
        composition,
        "create_publisher_client",
        build_publisher_client,
    )
    app = create_app(settings_factory=lambda: settings)
    assert app.dependency_overrides == {}

    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(
                app=app,  # pyright: ignore[reportArgumentType]
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://production.test",
            ) as client:
                liveness_response = await client.get("/health/live")
                readiness_response = await client.get("/health/ready")
                started = perf_counter()
                response = await client.post(
                    f"/webhooks/v1/grafana/{SOURCE_ID}",
                    content=EXAMPLE,
                    headers={
                        "Authorization": "Bearer accepted-token",
                        "Content-Type": "application/json",
                    },
                )
                elapsed = perf_counter() - started

                async with session_factory() as session:
                    incident_id = await session.scalar(
                        text(
                            "SELECT id FROM incidents ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                    alert_id = await session.scalar(
                        text(
                            "SELECT id FROM alert_instances ORDER BY last_seen_at DESC LIMIT 1"
                        )
                    )
                    artifact_counts = (
                        await session.execute(
                            text(
                                """SELECT
                                  (SELECT count(*) FROM incidents),
                                  (SELECT count(*) FROM rca_runs),
                                  (SELECT count(*) FROM worker_jobs),
                                  (SELECT count(*) FROM outbox_events)"""
                            )
                        )
                    ).one()
                incident_response = await client.get(f"/api/v1/incidents/{incident_id}")
                alert_response = await client.get(f"/api/v1/alerts/{alert_id}")

        assert response.status_code == 202
        assert liveness_response.status_code == 200
        assert liveness_response.json() == {"status": "ok"}
        assert readiness_response.status_code == 200
        assert readiness_statements == ["SELECT 1"]
        assert elapsed < 2
        assert artifact_counts == (1, 1, 1, 1)
        assert len(publisher_client.messages) == 1
        topic, payload, attributes = publisher_client.messages[0]
        message = RcaJobMessage.from_bytes(payload)
        assert topic == "projects/local-project/topics/rca-jobs"
        assert attributes == {"idempotencyKey": f"rca-run:{message.rca_run_id}"}
        assert incident_response.status_code == 200
        assert incident_response.json()["provider"] == "AWS"
        assert incident_response.json()["folderCode"] == "COM-LX-BOA-01"
        assert alert_response.status_code == 200
        assert alert_response.json()["provider"] == "AWS"
        assert alert_response.json()["severity"] == "SEV1"
        assert alert_response.json()["issue"]["rawText"].startswith(
            "Account: 123456789012"
        )
        async with session_factory() as session:
            delivery = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT raw_body, token_id, status
                        FROM webhook_deliveries
                        WHERE id = :delivery_id
                        """
                        ),
                        {"delivery_id": UUID(response.json()["deliveryId"])},
                    )
                )
                .mappings()
                .one()
            )
        assert delivery["raw_body"] == EXAMPLE
        assert delivery["token_id"] == "current-2026-08"
        assert delivery["status"] == "PROCESSED"
        assert publisher_client.stopped
    finally:
        await engine.dispose()
