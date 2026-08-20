import os
from pathlib import Path
from time import perf_counter
from uuid import UUID

import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr, TypeAdapter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sre_agent.api.main import create_app
from sre_agent.config.settings import Settings

SOURCE_ID = UUID("58000000-0000-0000-0000-000000000001")
TEAM_ID = UUID("18000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("28000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("38000000-0000-0000-0000-000000000001")
SERVICE_ID = UUID("48000000-0000-0000-0000-000000000001")
HTTP_URL = TypeAdapter(AnyHttpUrl)
DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
).replace("postgresql://", "postgresql+asyncpg://", 1)
EXAMPLE = (
    Path(__file__).resolve().parents[4] / "contracts/examples/grafana-firing-aws.json"
).read_bytes()


@pytest.mark.asyncio
async def test_production_resources_accept_and_commit_without_dependency_overrides():
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
        app_environment="local",
        model_name="test-model",
        metrics_mcp_url=HTTP_URL.validate_python("https://gateway/gcp/metrics/mcp"),
        trace_mcp_url=HTTP_URL.validate_python("https://gateway/gcp/trace/mcp"),
        log_mcp_url=HTTP_URL.validate_python("https://gateway/gcp/log/mcp"),
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
                        text("SELECT id FROM incidents ORDER BY created_at DESC LIMIT 1")
                    )
                    alert_id = await session.scalar(
                        text("SELECT id FROM alert_instances ORDER BY last_seen_at DESC LIMIT 1")
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
        assert elapsed < 2
        assert artifact_counts == (1, 1, 1, 1)
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
    finally:
        await engine.dispose()
