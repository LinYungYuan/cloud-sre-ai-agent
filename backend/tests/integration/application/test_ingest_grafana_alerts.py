import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sre_agent.application.alerts.ingest_grafana_alerts import (
    IngestGrafanaAlerts,
    IngestionResult,
)
from sre_agent.domain.alerts.classification import (
    AlertClassifier,
    ScopeField,
    ScopeResolver,
)
from sre_agent.persistence.repositories.jobs import SqlAlchemyJobRepository
from sre_agent.persistence.unit_of_work import SqlAlchemyUnitOfWork, UnitOfWork

TEAM_ID = UUID("10000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("20000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
SERVICE_ID = UUID("40000000-0000-0000-0000-000000000001")
SOURCE_ID = UUID("50000000-0000-0000-0000-000000000001")
TOKEN_ID = "current-2026-08"
RECEIVED_AT = datetime(2026, 8, 12, 4, 0, tzinfo=UTC)
DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/sre_agent",
).replace("postgresql://", "postgresql+asyncpg://", 1)


class KnownScope(ScopeResolver):
    def __init__(self, records: dict[tuple[ScopeField, str], UUID]) -> None:
        self._records = records

    def resolve(self, field: ScopeField, label_value: str) -> UUID | None:
        return self._records.get((field, label_value))


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE outbox_events, worker_jobs, rca_runs, "
                "incident_alerts, incidents, ingestion_dedup_keys, alert_instances, "
                "alert_events, webhook_deliveries, grafana_sources, services, "
                "environments, projects, teams CASCADE"
            )
        )
        parameters = {
            "team_id": TEAM_ID,
            "project_id": PROJECT_ID,
            "environment_id": ENVIRONMENT_ID,
            "service_id": SERVICE_ID,
            "source_id": SOURCE_ID,
        }
        for statement in (
            "INSERT INTO teams (id, name) VALUES (:team_id, 'payments')",
            (
                "INSERT INTO projects (id, team_id, name) "
                "VALUES (:project_id, :team_id, 'checkout')"
            ),
            (
                "INSERT INTO environments (id, project_id, name) "
                "VALUES (:environment_id, :project_id, 'production')"
            ),
            (
                "INSERT INTO services (id, environment_id, name) "
                "VALUES (:service_id, :environment_id, 'api')"
            ),
            (
                "INSERT INTO grafana_sources (id, project_id, environment_id, name) "
                "VALUES (:source_id, :project_id, :environment_id, 'primary')"
            ),
        ):
            await connection.execute(text(statement), parameters)
    try:
        yield factory
    finally:
        await engine.dispose()


def _classifier(*, classified: bool = True) -> AlertClassifier:
    records: dict[tuple[ScopeField, str], UUID] = (
        {
            ("team", "payments"): TEAM_ID,
            ("project", "checkout"): PROJECT_ID,
            ("environment", "production"): ENVIRONMENT_ID,
            ("service", "api"): SERVICE_ID,
        }
        if classified
        else {}
    )
    return AlertClassifier(SOURCE_ID, KnownScope(records), [])


def _use_case(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    classified: bool = True,
    uow_factory: Callable[[], UnitOfWork] | None = None,
) -> IngestGrafanaAlerts:
    return IngestGrafanaAlerts(
        uow_factory=uow_factory or (lambda: SqlAlchemyUnitOfWork(session_factory)),
        classifier=_classifier(classified=classified),
        max_body_bytes=1_048_576,
    )


def _alert(
    fingerprint: str,
    *,
    status: str = "firing",
    value: int = 95,
    classified: bool = True,
) -> dict[str, object]:
    labels = {"alertname": "HighCPU"}
    if classified:
        labels |= {
            "team": "payments",
            "project": "checkout",
            "environment": "production",
            "service": "api",
        }
    return {
        "status": status,
        "labels": labels,
        "annotations": {"summary": "API CPU is high"},
        "startsAt": "2026-08-12T02:00:00Z",
        "endsAt": "2026-08-12T03:00:00Z",
        "values": {"A": value},
        "generatorURL": "https://grafana.example.com/alert/api",
        "fingerprint": fingerprint,
        "vendorExtension": {"kept": True},
    }


def _body(*alerts: dict[str, object]) -> bytes:
    return json.dumps(
        {"receiver": "sre-agent", "status": alerts[0]["status"], "alerts": alerts},
        indent=2,
        ensure_ascii=False,
    ).encode()


async def _rows(
    session_factory: async_sessionmaker[AsyncSession], query: str
) -> list[dict[str, Any]]:
    async with session_factory() as session:
        result = await session.execute(text(query))
        return [dict(row) for row in result.mappings().all()]


@pytest.mark.asyncio
async def test_new_firing_is_stored_atomically_with_one_queued_rca_and_outbox(
    session_factory,
):
    raw_body = _body(_alert("grafana-api"))

    result = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, raw_body, RECEIVED_AT
    )

    assert isinstance(result, IngestionResult)
    assert result.accepted_at == RECEIVED_AT
    assert len(result.incident_ids) == 1
    delivery = (await _rows(session_factory, "SELECT * FROM webhook_deliveries"))[0]
    assert delivery["id"] == result.delivery_id
    assert delivery["status"] == "PROCESSED"
    assert delivery["body_hash"] == hashlib.sha256(raw_body).hexdigest()
    assert delivery["raw_payload"] == json.loads(raw_body)
    assert delivery["token_id"] == TOKEN_ID
    event = (await _rows(session_factory, "SELECT * FROM alert_events"))[0]
    assert event["raw_payload"] == json.loads(raw_body)["alerts"][0]
    assert event["alert_state"] == "FIRING"
    incident = (await _rows(session_factory, "SELECT * FROM incidents"))[0]
    assert incident["id"] == result.incident_ids[0]
    assert (incident["status"], incident["alert_state"]) == ("OPEN", "FIRING")
    assert (
        incident["team_id"],
        incident["project_id"],
        incident["environment_id"],
        incident["service_id"],
    ) == (TEAM_ID, PROJECT_ID, ENVIRONMENT_ID, SERVICE_ID)
    runs = await _rows(session_factory, "SELECT * FROM rca_runs")
    jobs = await _rows(session_factory, "SELECT * FROM worker_jobs")
    outbox = await _rows(session_factory, "SELECT * FROM outbox_events")
    assert len(runs) == len(jobs) == len(outbox) == 1
    assert runs[0]["status"] == "QUEUED"
    assert jobs[0]["rca_run_id"] == runs[0]["id"]
    assert outbox[0]["idempotency_key"] == f"rca-run:{runs[0]['id']}"


@pytest.mark.asyncio
async def test_identical_redelivery_keeps_duplicate_delivery_without_repeating_transitions(
    session_factory,
):
    raw_body = _body(_alert("grafana-api"))
    first = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, raw_body, RECEIVED_AT
    )

    duplicate = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, raw_body, RECEIVED_AT + timedelta(seconds=1)
    )

    assert duplicate.delivery_id != first.delivery_id
    assert duplicate.incident_ids == ()
    deliveries = await _rows(
        session_factory, "SELECT status FROM webhook_deliveries ORDER BY received_at"
    )
    assert [row["status"] for row in deliveries] == ["PROCESSED", "DUPLICATE"]
    for table in (
        "ingestion_dedup_keys",
        "alert_events",
        "alert_instances",
        "incidents",
        "incident_alerts",
        "rca_runs",
        "worker_jobs",
        "outbox_events",
    ):
        rows = await _rows(session_factory, f"SELECT * FROM {table}")
        assert len(rows) == 1, table
    instance = (await _rows(session_factory, "SELECT * FROM alert_instances"))[0]
    assert instance["version"] == 1


@pytest.mark.asyncio
async def test_firing_update_refreshes_instance_and_reuses_active_incident(
    session_factory,
):
    first = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _body(_alert("grafana-api")), RECEIVED_AT
    )

    updated = await _use_case(session_factory).execute(
        SOURCE_ID,
        TOKEN_ID,
        _body(_alert("grafana-api", value=99)),
        RECEIVED_AT + timedelta(minutes=1),
    )

    assert updated.incident_ids == first.incident_ids
    instance = (await _rows(session_factory, "SELECT * FROM alert_instances"))[0]
    assert instance["version"] == 2
    assert instance["annotations"] == {"summary": "API CPU is high"}
    assert len(await _rows(session_factory, "SELECT * FROM alert_events")) == 2
    assert len(await _rows(session_factory, "SELECT * FROM incident_alerts")) == 2
    assert len(await _rows(session_factory, "SELECT * FROM incidents")) == 1
    assert len(await _rows(session_factory, "SELECT * FROM rca_runs")) == 1
    assert len(await _rows(session_factory, "SELECT * FROM outbox_events")) == 1


@pytest.mark.asyncio
async def test_resolved_updates_machine_state_without_changing_human_incident_status(
    session_factory,
):
    first = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _body(_alert("grafana-api")), RECEIVED_AT
    )

    resolved = await _use_case(session_factory).execute(
        SOURCE_ID,
        TOKEN_ID,
        _body(_alert("grafana-api", status="resolved", value=20)),
        RECEIVED_AT + timedelta(minutes=2),
    )

    assert resolved.incident_ids == first.incident_ids
    instance = (await _rows(session_factory, "SELECT * FROM alert_instances"))[0]
    assert instance["state"] == "RESOLVED"
    assert instance["resolved_at"] == RECEIVED_AT + timedelta(minutes=2)
    incident = (await _rows(session_factory, "SELECT * FROM incidents"))[0]
    assert incident["alert_state"] == "RESOLVED"
    assert incident["status"] == "OPEN"
    assert incident["resolved_at"] is None
    assert len(await _rows(session_factory, "SELECT * FROM rca_runs")) == 1


@pytest.mark.asyncio
async def test_firing_after_manual_resolution_creates_a_new_incident(session_factory):
    first = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _body(_alert("grafana-api")), RECEIVED_AT
    )
    async with session_factory.begin() as session:
        await session.execute(
            text(
                "UPDATE incidents SET status = 'RESOLVED', resolved_at = :resolved_at "
                "WHERE id = :incident_id"
            ),
            {
                "resolved_at": RECEIVED_AT + timedelta(minutes=3),
                "incident_id": first.incident_ids[0],
            },
        )

    reopened = await _use_case(session_factory).execute(
        SOURCE_ID,
        TOKEN_ID,
        _body(_alert("grafana-api", value=100)),
        RECEIVED_AT + timedelta(minutes=4),
    )

    assert reopened.incident_ids != first.incident_ids
    incidents = await _rows(
        session_factory, "SELECT * FROM incidents ORDER BY created_at, id"
    )
    assert len(incidents) == 2
    new_incident = next(
        row for row in incidents if row["id"] == reopened.incident_ids[0]
    )
    assert new_incident["status"] == "OPEN"
    assert new_incident["reopened_from_incident_id"] == first.incident_ids[0]
    assert len(await _rows(session_factory, "SELECT * FROM rca_runs")) == 2
    assert len(await _rows(session_factory, "SELECT * FROM worker_jobs")) == 2
    assert len(await _rows(session_factory, "SELECT * FROM outbox_events")) == 2


@pytest.mark.asyncio
async def test_grouped_firing_alerts_share_one_active_incident(session_factory):
    result = await _use_case(session_factory).execute(
        SOURCE_ID,
        TOKEN_ID,
        _body(_alert("grafana-api"), _alert("grafana-worker", value=88)),
        RECEIVED_AT,
    )

    assert len(result.incident_ids) == 1
    assert len(await _rows(session_factory, "SELECT * FROM alert_events")) == 2
    assert len(await _rows(session_factory, "SELECT * FROM alert_instances")) == 2
    assert len(await _rows(session_factory, "SELECT * FROM incident_alerts")) == 2
    assert len(await _rows(session_factory, "SELECT * FROM incidents")) == 1
    assert len(await _rows(session_factory, "SELECT * FROM rca_runs")) == 1
    assert len(await _rows(session_factory, "SELECT * FROM outbox_events")) == 1


@pytest.mark.asyncio
async def test_unclassified_alert_uses_source_scope_and_waits_for_classification(
    session_factory,
):
    result = await _use_case(session_factory, classified=False).execute(
        SOURCE_ID,
        TOKEN_ID,
        _body(_alert("grafana-api", classified=False)),
        RECEIVED_AT,
    )

    assert len(result.incident_ids) == 1
    incident = (await _rows(session_factory, "SELECT * FROM incidents"))[0]
    assert (
        incident["team_id"],
        incident["project_id"],
        incident["environment_id"],
        incident["service_id"],
    ) == (TEAM_ID, PROJECT_ID, ENVIRONMENT_ID, None)
    run = (await _rows(session_factory, "SELECT * FROM rca_runs"))[0]
    assert run["status"] == "WAITING_FOR_CLASSIFICATION"
    assert len(await _rows(session_factory, "SELECT * FROM worker_jobs")) == 1
    assert len(await _rows(session_factory, "SELECT * FROM outbox_events")) == 1


class FailingJobRepository(SqlAlchemyJobRepository):
    async def create_rca_work(
        self,
        *,
        incident_id: UUID,
        run_status: str,
        available_at: datetime,
    ) -> UUID:
        raise RuntimeError("injected failure after Incident insertion")


@pytest.mark.asyncio
async def test_failure_after_incident_insert_rolls_back_entire_delivery(
    session_factory,
):
    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(
            session_factory,
            jobs_repository_factory=FailingJobRepository,
        )

    with pytest.raises(RuntimeError, match="injected failure"):
        await _use_case(session_factory, uow_factory=uow_factory).execute(
            SOURCE_ID, TOKEN_ID, _body(_alert("grafana-api")), RECEIVED_AT
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
        assert await _rows(session_factory, f"SELECT * FROM {table}") == [], table
