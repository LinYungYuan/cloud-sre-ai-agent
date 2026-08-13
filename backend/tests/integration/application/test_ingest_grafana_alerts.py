import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sre_agent.application.alerts.ingest_grafana_alerts import IngestGrafanaAlerts
from sre_agent.domain.alerts.normalization import (
    NormalizationRule,
    RuleCondition,
    RuleOutput,
    SafeRuleEngine,
)
from sre_agent.domain.alerts.provider import Provider
from sre_agent.persistence.repositories.incidents import IncidentScope
from sre_agent.persistence.repositories.normalization import (
    FolderScopeProvider,
    NormalizationRuleProvider,
)
from sre_agent.persistence.unit_of_work import SqlAlchemyUnitOfWork

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
)
ROOT = Path(__file__).resolve().parents[4]
AWS_FIXTURE = (ROOT / "contracts/examples/grafana-firing-aws.json").read_bytes()
GCP_FIXTURE = (ROOT / "contracts/examples/grafana-firing.json").read_bytes()
SOURCE_ID = UUID("50000000-0000-0000-0000-000000000001")
TEAM_ID = UUID("10000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("20000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
SERVICE_ID = UUID("40000000-0000-0000-0000-000000000001")
RECEIVED_AT = datetime(2026, 8, 13, 7, tzinfo=UTC)
TOKEN_ID = "current-2026-08"


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE outbox_events, teams CASCADE"))
        await connection.execute(
            text("INSERT INTO teams (id, name) VALUES (:id, 'ingestion-team')"),
            {"id": TEAM_ID},
        )
        await connection.execute(
            text(
                "INSERT INTO projects (id, team_id, name) "
                "VALUES (:id, :team_id, 'ingestion-project')"
            ),
            {"id": PROJECT_ID, "team_id": TEAM_ID},
        )
        await connection.execute(
            text(
                "INSERT INTO environments (id, project_id, name) "
                "VALUES (:id, :project_id, 'ingestion-environment')"
            ),
            {"id": ENVIRONMENT_ID, "project_id": PROJECT_ID},
        )
        await connection.execute(
            text(
                "INSERT INTO services (id, environment_id, name) "
                "VALUES (:id, :environment_id, 'ingestion-service')"
            ),
            {"id": SERVICE_ID, "environment_id": ENVIRONMENT_ID},
        )
        await connection.execute(
            text(
                "INSERT INTO grafana_sources "
                "(id, project_id, environment_id, name) "
                "VALUES (:id, :project_id, :environment_id, 'grafana')"
            ),
            {
                "id": SOURCE_ID,
                "project_id": PROJECT_ID,
                "environment_id": ENVIRONMENT_ID,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO normalization_rules (
                    id, source_id, name, version, priority, provider,
                    conditions, output
                ) VALUES
                (
                    '60000000-0000-0000-0000-000000000001', :source_id,
                    'gcp-project', 1, 1, 'GCP', '[]'::jsonb,
                    '{"provider":"GCP","resource_type":"gcp_resource"}'::jsonb
                ),
                (
                    '60000000-0000-0000-0000-000000000002', :source_id,
                    'aws-account', 1, 2, 'AWS', '[]'::jsonb,
                    '{"provider":"AWS","resource_type":"rds_instance"}'::jsonb
                )
                """
            ),
            {"source_id": SOURCE_ID},
        )
    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE TABLE outbox_events, teams CASCADE")
            )
        await engine.dispose()


def _rule_provider() -> NormalizationRuleProvider:
    gcp = NormalizationRule(
        id=UUID("60000000-0000-0000-0000-000000000001"),
        name="gcp-project",
        version=1,
        priority=1,
        conditions=(
            RuleCondition(
                path="labels.resource.label.project_id",
                operator="exists",
            ),
        ),
        output=RuleOutput(
            provider=Provider.GCP,
            resource_type="gcp_resource",
            scope_path="labels.resource.label.project_id",
            resource_id_path="labels.DBInstanceIdentifier",
        ),
    )
    aws = NormalizationRule(
        id=UUID("60000000-0000-0000-0000-000000000002"),
        name="aws-account",
        version=1,
        priority=2,
        conditions=(
            RuleCondition(
                path="labels.Series", operator="format", value="aws_account_id"
            ),
        ),
        output=RuleOutput(
            provider=Provider.AWS,
            resource_type="rds_instance",
            scope_path="labels.Series",
            resource_id_path="labels.DBInstanceIdentifier",
        ),
    )
    return NormalizationRuleProvider(
        {SOURCE_ID: SafeRuleEngine((gcp, aws))},
        frozenset({SOURCE_ID}),
    )


def _use_case(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    folders: FolderScopeProvider | None = None,
    uow_factory=None,
) -> IngestGrafanaAlerts:
    return IngestGrafanaAlerts(
        uow_factory=uow_factory or (lambda: SqlAlchemyUnitOfWork(session_factory)),
        normalization_rule_provider=_rule_provider(),
        folder_scope_provider=folders or FolderScopeProvider({}),
        max_body_bytes=1_048_576,
    )


async def _rows(
    session_factory: async_sessionmaker[AsyncSession], statement: str
) -> list[dict]:
    async with session_factory() as session:
        return [
            dict(row) for row in (await session.execute(text(statement))).mappings()
        ]


def _payload(raw: bytes) -> dict:
    return json.loads(raw)


def _encode(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


@pytest.mark.asyncio
async def test_approved_aws_body_persists_exact_issue_and_always_creates_rca(
    session_factory,
) -> None:
    result = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, AWS_FIXTURE, RECEIVED_AT
    )

    assert len(result.incident_ids) == 1
    delivery = (await _rows(session_factory, "SELECT * FROM webhook_deliveries"))[0]
    event = (await _rows(session_factory, "SELECT * FROM alert_events"))[0]
    incident = (await _rows(session_factory, "SELECT * FROM incidents"))[0]
    run = (await _rows(session_factory, "SELECT * FROM rca_runs"))[0]
    job = (await _rows(session_factory, "SELECT * FROM worker_jobs"))[0]
    outbox = (await _rows(session_factory, "SELECT * FROM outbox_events"))[0]

    assert delivery["status"] == "PROCESSED"
    assert delivery["raw_body"] == AWS_FIXTURE
    assert delivery["truncated_alerts"] == 0
    assert delivery["incomplete"] is False
    assert event["provider"] == "AWS"
    assert event["folder_code"] == "COM-LX-BOA-01"
    assert event["alert_name"] == "High CPU usage"
    assert event["severity_raw"] == "ERROR"
    assert event["severity_canonical"] == "SEV1"
    assert event["issue"] == {
        "rawText": "Account: 123456789012\nDB Name: production-rds-01\nValue: 85.23%\n<br>",
        "source": "grafana.annotations.AlertValues",
        "contentType": "text/plain",
        "untrusted": True,
    }
    assert event["normalization_status"] == "NORMALIZED"
    assert incident["identity_version"] == 2
    assert incident["provider"] == "AWS"
    assert all(
        incident[column] is None
        for column in ("team_id", "project_id", "environment_id", "service_id")
    )
    assert run["status"] == "QUEUED"
    assert job["job_type"] == "RCA_ANALYSIS"
    assert outbox["event_type"] == "RCA_RUN_REQUESTED"
    assert (
        job["payload"]
        == outbox["payload"]
        == {
            "schemaVersion": 1,
            "workerJobId": str(job["id"]),
            "rcaRunId": str(run["id"]),
            "incidentId": str(incident["id"]),
            "attempt": 1,
        }
    )


@pytest.mark.asyncio
async def test_gcp_project_key_is_the_only_provider_discriminator(
    session_factory,
) -> None:
    await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, GCP_FIXTURE, RECEIVED_AT
    )

    event = (await _rows(session_factory, "SELECT * FROM alert_events"))[0]
    incident = (await _rows(session_factory, "SELECT * FROM incidents"))[0]
    assert event["provider"] == "GCP"
    assert event["normalization_status"] == "NORMALIZED"
    assert incident["provider"] == "GCP"


@pytest.mark.asyncio
async def test_present_blank_gcp_project_is_validation_failed_but_still_has_rca(
    session_factory,
) -> None:
    payload = _payload(GCP_FIXTURE)
    payload["alerts"][0]["labels"]["resource.label.project_id"] = "  "

    await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _encode(payload), RECEIVED_AT
    )

    delivery = (await _rows(session_factory, "SELECT * FROM webhook_deliveries"))[0]
    event = (await _rows(session_factory, "SELECT * FROM alert_events"))[0]
    assert delivery["status"] == "VALIDATION_FAILED"
    assert event["provider"] == "GCP"
    assert event["validation_errors"] == [
        {"field": "resource.label.project_id", "code": "invalid_value"}
    ]
    assert len(await _rows(session_factory, "SELECT id FROM incidents")) == 1
    assert len(await _rows(session_factory, "SELECT id FROM rca_runs")) == 1
    assert len(await _rows(session_factory, "SELECT id FROM worker_jobs")) == 1
    assert len(await _rows(session_factory, "SELECT id FROM outbox_events")) == 1


@pytest.mark.asyncio
async def test_folder_mapping_is_optional_but_used_when_present(
    session_factory,
) -> None:
    folders = FolderScopeProvider(
        {
            (SOURCE_ID, "COM-LX-BOA-01"): IncidentScope(
                TEAM_ID, PROJECT_ID, ENVIRONMENT_ID, SERVICE_ID
            )
        }
    )

    await _use_case(session_factory, folders=folders).execute(
        SOURCE_ID, TOKEN_ID, AWS_FIXTURE, RECEIVED_AT
    )

    incident = (await _rows(session_factory, "SELECT * FROM incidents"))[0]
    assert (
        incident["team_id"],
        incident["project_id"],
        incident["environment_id"],
        incident["service_id"],
    ) == (TEAM_ID, PROJECT_ID, ENVIRONMENT_ID, SERVICE_ID)


@pytest.mark.asyncio
async def test_duplicate_delivery_keeps_second_delivery_without_repeating_work(
    session_factory,
) -> None:
    use_case = _use_case(session_factory)
    first = await use_case.execute(SOURCE_ID, TOKEN_ID, AWS_FIXTURE, RECEIVED_AT)
    second = await use_case.execute(SOURCE_ID, TOKEN_ID, AWS_FIXTURE, RECEIVED_AT)

    assert first.delivery_id != second.delivery_id
    assert [
        row["status"]
        for row in await _rows(
            session_factory,
            "SELECT status FROM webhook_deliveries "
            "ORDER BY CASE status WHEN 'PROCESSED' THEN 1 ELSE 2 END",
        )
    ] == ["PROCESSED", "DUPLICATE"]
    assert len(await _rows(session_factory, "SELECT id FROM alert_events")) == 1
    assert len(await _rows(session_factory, "SELECT id FROM incidents")) == 1
    assert len(await _rows(session_factory, "SELECT id FROM rca_runs")) == 1


@pytest.mark.asyncio
async def test_identity_v2_groups_only_same_source_folder_and_alert_name(
    session_factory,
) -> None:
    first = _payload(AWS_FIXTURE)
    update = _payload(AWS_FIXTURE)
    update["alerts"][0]["fingerprint"] = "different-fingerprint"
    other = _payload(AWS_FIXTURE)
    other["alerts"][0]["fingerprint"] = "other-alert"
    other["alerts"][0]["labels"]["alertname"] = "Database connections high"

    use_case = _use_case(session_factory)
    await use_case.execute(SOURCE_ID, TOKEN_ID, _encode(first), RECEIVED_AT)
    await use_case.execute(SOURCE_ID, TOKEN_ID, _encode(update), RECEIVED_AT)
    await use_case.execute(SOURCE_ID, TOKEN_ID, _encode(other), RECEIVED_AT)

    incidents = await _rows(
        session_factory,
        "SELECT alert_name, identity_version FROM incidents ORDER BY alert_name",
    )
    assert incidents == [
        {"alert_name": "Database connections high", "identity_version": 2},
        {"alert_name": "High CPU usage", "identity_version": 2},
    ]
    assert len(await _rows(session_factory, "SELECT id FROM rca_runs")) == 2


@pytest.mark.asyncio
async def test_grouped_webhook_normalizes_each_alert_provider_independently(
    session_factory,
) -> None:
    aws = _payload(AWS_FIXTURE)["alerts"][0]
    gcp = _payload(GCP_FIXTURE)["alerts"][0]
    gcp["labels"]["alertname"] = "GCP High CPU usage"
    gcp["fingerprint"] = "gcp-fingerprint"
    payload = _payload(AWS_FIXTURE)
    payload["alerts"] = [aws, gcp]

    await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _encode(payload), RECEIVED_AT
    )

    events = await _rows(
        session_factory,
        "SELECT provider, issue->>'rawText' AS issue FROM alert_events ORDER BY provider",
    )
    assert [row["provider"] for row in events] == ["AWS", "GCP"]
    assert all(row["issue"].startswith("Account:") for row in events)
    assert len(await _rows(session_factory, "SELECT id FROM incidents")) == 2


@pytest.mark.asyncio
async def test_concurrent_same_identity_has_one_active_incident_and_one_rca(
    session_factory,
) -> None:
    second = _payload(AWS_FIXTURE)
    second["alerts"][0]["fingerprint"] = "concurrent-second"
    start = asyncio.Event()

    async def ingest(raw: bytes) -> None:
        await start.wait()
        await _use_case(session_factory).execute(SOURCE_ID, TOKEN_ID, raw, RECEIVED_AT)

    tasks = [
        asyncio.create_task(ingest(AWS_FIXTURE)),
        asyncio.create_task(ingest(_encode(second))),
    ]
    start.set()
    await asyncio.gather(*tasks)

    assert len(await _rows(session_factory, "SELECT id FROM incidents")) == 1
    assert len(await _rows(session_factory, "SELECT id FROM rca_runs")) == 1
    assert len(await _rows(session_factory, "SELECT id FROM worker_jobs")) == 1
    assert len(await _rows(session_factory, "SELECT id FROM outbox_events")) == 1


class FailingJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_rca_work(self, **values) -> UUID:
        del values
        raise RuntimeError("injected failure")


@pytest.mark.asyncio
async def test_failure_after_incident_insert_rolls_back_every_artifact(
    session_factory,
) -> None:
    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(
            session_factory,
            jobs_repository_factory=FailingJobRepository,
        )

    with pytest.raises(RuntimeError, match="injected failure"):
        await _use_case(session_factory, uow_factory=uow_factory).execute(
            SOURCE_ID, TOKEN_ID, AWS_FIXTURE, RECEIVED_AT
        )

    for table in (
        "webhook_deliveries",
        "ingestion_dedup_keys",
        "alert_events",
        "alert_instances",
        "incidents",
        "incident_alerts",
        "rca_runs",
        "worker_jobs",
        "outbox_events",
    ):
        assert await _rows(session_factory, f"SELECT id FROM {table}") == []
