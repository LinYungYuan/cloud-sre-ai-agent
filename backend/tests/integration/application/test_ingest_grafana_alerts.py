import asyncio
import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Self
from uuid import UUID, uuid4

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
from sre_agent.persistence.repositories.alerts import (
    AlertRepository,
    SqlAlchemyAlertRepository,
)
from sre_agent.persistence.repositories.incidents import (
    IncidentRepository,
    SqlAlchemyIncidentRepository,
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

CROSS_CLOUD_REQUIRED_LABELS = (
    "alertname",
    "cloud_provider",
    "cloud_scope_id",
    "resource_type",
    "resource_id",
    "environment",
    "service",
    "team",
    "severity",
    "signal_type",
)

GCP_LABELS = {
    "alertname": "HighCPU",
    "cloud_provider": "gcp",
    "cloud_scope_id": "checkout-prod",
    "resource_type": "gke_service",
    "resource_id": "projects/checkout-prod/locations/asia-east1/services/api",
    "environment": "production",
    "service": "api",
    "team": "payments",
    "severity": "warning",
    "signal_type": "metric",
    "project": "checkout",
}

AWS_LABELS = {
    "alertname": "RdsCpuHigh",
    "cloud_provider": "aws",
    "cloud_scope_id": "123456789012",
    "resource_type": "rds_instance",
    "resource_id": "arn:aws:rds:ap-northeast-1:123456789012:db:orders-prod",
    "environment": "production",
    "service": "api",
    "team": "payments",
    "severity": "critical",
    "signal_type": "metric",
    "project": "checkout",
}

DOWNSTREAM_TABLES = (
    "alert_instances",
    "incidents",
    "incident_alerts",
    "rca_runs",
    "worker_jobs",
    "outbox_events",
)


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


def _classifier() -> AlertClassifier:
    records: dict[tuple[ScopeField, str], UUID] = {
        ("team", "payments"): TEAM_ID,
        ("project", "checkout"): PROJECT_ID,
        ("environment", "production"): ENVIRONMENT_ID,
        ("service", "api"): SERVICE_ID,
    }
    return AlertClassifier(SOURCE_ID, KnownScope(records), [])


def _use_case(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uow_factory: Callable[[], UnitOfWork] | None = None,
) -> IngestGrafanaAlerts:
    return IngestGrafanaAlerts(
        uow_factory=uow_factory or (lambda: SqlAlchemyUnitOfWork(session_factory)),
        classifier=_classifier(),
        max_body_bytes=1_048_576,
    )


def _alert(
    fingerprint: str,
    *,
    status: str = "firing",
    value: int = 95,
    cloud_provider: str = "gcp",
    label_overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    labels = dict(AWS_LABELS if cloud_provider == "aws" else GCP_LABELS)
    labels.update(label_overrides or {})
    return {
        "status": status,
        "labels": labels,
        "annotations": {"summary": f"{labels['alertname']} is firing"},
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


async def _assert_table_counts(
    session_factory: async_sessionmaker[AsyncSession],
    expected: dict[str, int],
) -> None:
    for table_name, count in expected.items():
        rows = await _rows(session_factory, f"SELECT * FROM {table_name}")
        assert len(rows) == count, table_name


async def _assert_validation_retention(
    session_factory: async_sessionmaker[AsyncSession],
    result: IngestionResult,
    raw_alert: dict[str, object],
    expected_errors: list[dict[str, str]],
) -> None:
    assert result.incident_ids == ()
    delivery = (await _rows(session_factory, "SELECT * FROM webhook_deliveries"))[0]
    assert delivery["id"] == result.delivery_id
    assert delivery["status"] == "VALIDATION_FAILED"
    event = (await _rows(session_factory, "SELECT * FROM alert_events"))[0]
    assert event["validation_status"] == "VALIDATION_FAILED"
    assert event["validation_errors"] == expected_errors
    assert event["raw_payload"] == raw_alert
    assert len(await _rows(session_factory, "SELECT * FROM ingestion_dedup_keys")) == 1
    await _assert_table_counts(
        session_factory,
        {table_name: 0 for table_name in DOWNSTREAM_TABLES},
    )


class DedupBarrierAlertRepository(SqlAlchemyAlertRepository):
    def __init__(self, session: AsyncSession, barrier: asyncio.Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier

    async def claim_dedup_key(
        self,
        *,
        source_id: UUID,
        dedup_key: str,
        delivery_id: UUID,
        delivery_partition_timestamp: datetime,
    ) -> bool:
        await self._barrier.wait()
        return await super().claim_dedup_key(
            source_id=source_id,
            dedup_key=dedup_key,
            delivery_id=delivery_id,
            delivery_partition_timestamp=delivery_partition_timestamp,
        )


class IncidentBarrierRepository(SqlAlchemyIncidentRepository):
    def __init__(self, session: AsyncSession, barrier: asyncio.Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier

    async def get_or_create_active(self, *args: Any, **kwargs: Any) -> Any:
        await self._barrier.wait()
        return await super().get_or_create_active(*args, **kwargs)


class RepositoryBarrierUnitOfWork(SqlAlchemyUnitOfWork):
    alerts: AlertRepository
    incidents: IncidentRepository

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dedup_barrier: asyncio.Barrier | None = None,
        incident_barrier: asyncio.Barrier | None = None,
    ) -> None:
        super().__init__(session_factory)
        self._dedup_barrier = dedup_barrier
        self._incident_barrier = incident_barrier

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        assert self._session is not None
        if self._dedup_barrier is not None:
            self.alerts = DedupBarrierAlertRepository(
                self._session, self._dedup_barrier
            )
        if self._incident_barrier is not None:
            self.incidents = IncidentBarrierRepository(
                self._session, self._incident_barrier
            )
        return self


class FailingJobRepository(SqlAlchemyJobRepository):
    async def create_rca_work(
        self,
        *,
        incident_id: UUID,
        run_status: str,
        available_at: datetime,
    ) -> UUID:
        raise RuntimeError("injected failure after Incident insertion")


class BarrierJobRepository(SqlAlchemyJobRepository):
    def __init__(self, session: AsyncSession, barrier: asyncio.Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier

    async def create_rca_work(
        self,
        *,
        incident_id: UUID,
        run_status: str,
        available_at: datetime,
    ) -> UUID:
        await self._barrier.wait()
        return await super().create_rca_work(
            incident_id=incident_id,
            run_status=run_status,
            available_at=available_at,
        )


@pytest.mark.asyncio
async def test_new_gcp_firing_is_stored_atomically_with_validation_and_one_rca(
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
    assert event["validation_status"] == "VALID"
    assert event["validation_errors"] == []
    incident = (await _rows(session_factory, "SELECT * FROM incidents"))[0]
    assert incident["id"] == result.incident_ids[0]
    assert (incident["status"], incident["alert_state"]) == ("OPEN", "FIRING")
    assert len(incident["identity_key"]) == 64
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
@pytest.mark.parametrize(
    ("cloud_provider", "severity", "expected_severity"),
    [
        ("gcp", "critical", "SEV1"),
        ("aws", "warning", "SEV3"),
        ("gcp", "info", "SEV4"),
    ],
)
async def test_gcp_and_aws_firing_create_queued_rca_with_exact_severity(
    session_factory,
    cloud_provider: str,
    severity: str,
    expected_severity: str,
):
    alert = _alert(
        f"{cloud_provider}-{severity}",
        cloud_provider=cloud_provider,
        label_overrides={"severity": severity},
    )

    result = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _body(alert), RECEIVED_AT
    )

    assert len(result.incident_ids) == 1
    incident = (await _rows(session_factory, "SELECT * FROM incidents"))[0]
    assert incident["severity"] == expected_severity
    run = (await _rows(session_factory, "SELECT * FROM rca_runs"))[0]
    assert run["status"] == "QUEUED"
    assert len(await _rows(session_factory, "SELECT * FROM worker_jobs")) == 1
    assert len(await _rows(session_factory, "SELECT * FROM outbox_events")) == 1


@pytest.mark.asyncio
async def test_identical_redelivery_keeps_delivery_without_repeating_transitions(
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
    await _assert_table_counts(
        session_factory,
        {
            "ingestion_dedup_keys": 1,
            "alert_events": 1,
            "alert_instances": 1,
            "incidents": 1,
            "incident_alerts": 1,
            "rca_runs": 1,
            "worker_jobs": 1,
            "outbox_events": 1,
        },
    )
    instance = (await _rows(session_factory, "SELECT * FROM alert_instances"))[0]
    assert instance["version"] == 1


@pytest.mark.asyncio
async def test_firing_update_reuses_only_the_same_identity_active_incident(
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
    await _assert_table_counts(
        session_factory,
        {
            "alert_events": 2,
            "incident_alerts": 2,
            "incidents": 1,
            "rca_runs": 1,
            "worker_jobs": 1,
            "outbox_events": 1,
        },
    )


@pytest.mark.asyncio
async def test_same_resource_with_different_alertname_creates_separate_work(
    session_factory,
):
    first = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _body(_alert("cpu")), RECEIVED_AT
    )
    second = await _use_case(session_factory).execute(
        SOURCE_ID,
        TOKEN_ID,
        _body(_alert("memory", label_overrides={"alertname": "HighMemory"})),
        RECEIVED_AT + timedelta(minutes=1),
    )

    assert first.incident_ids != second.incident_ids
    await _assert_table_counts(
        session_factory,
        {
            "incidents": 2,
            "rca_runs": 2,
            "worker_jobs": 2,
            "outbox_events": 2,
        },
    )
    identities = {
        row["identity_key"]
        for row in await _rows(session_factory, "SELECT identity_key FROM incidents")
    }
    assert len(identities) == 2


@pytest.mark.asyncio
async def test_same_project_with_different_resource_id_creates_separate_work(
    session_factory,
):
    first = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _body(_alert("api")), RECEIVED_AT
    )
    second = await _use_case(session_factory).execute(
        SOURCE_ID,
        TOKEN_ID,
        _body(
            _alert(
                "worker",
                label_overrides={
                    "resource_id": (
                        "projects/checkout-prod/locations/asia-east1/services/worker"
                    )
                },
            )
        ),
        RECEIVED_AT + timedelta(minutes=1),
    )

    assert first.incident_ids != second.incident_ids
    await _assert_table_counts(
        session_factory,
        {
            "incidents": 2,
            "rca_runs": 2,
            "worker_jobs": 2,
            "outbox_events": 2,
        },
    )


@pytest.mark.asyncio
async def test_one_webhook_with_two_identities_creates_two_incidents_and_work(
    session_factory,
):
    result = await _use_case(session_factory).execute(
        SOURCE_ID,
        TOKEN_ID,
        _body(
            _alert("api"),
            _alert(
                "worker",
                value=88,
                label_overrides={
                    "resource_id": (
                        "projects/checkout-prod/locations/asia-east1/services/worker"
                    )
                },
            ),
        ),
        RECEIVED_AT,
    )

    assert len(result.incident_ids) == 2
    assert len(set(result.incident_ids)) == 2
    await _assert_table_counts(
        session_factory,
        {
            "alert_events": 2,
            "alert_instances": 2,
            "incident_alerts": 2,
            "incidents": 2,
            "rca_runs": 2,
            "worker_jobs": 2,
            "outbox_events": 2,
        },
    )


@pytest.mark.asyncio
async def test_reopen_points_to_latest_resolved_incident_of_exact_identity(
    session_factory,
):
    first = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _body(_alert("cpu")), RECEIVED_AT
    )
    other = await _use_case(session_factory).execute(
        SOURCE_ID,
        TOKEN_ID,
        _body(_alert("memory", label_overrides={"alertname": "HighMemory"})),
        RECEIVED_AT + timedelta(minutes=1),
    )
    async with session_factory.begin() as session:
        await session.execute(
            text(
                "UPDATE incidents SET status = 'RESOLVED', resolved_at = :resolved_at "
                "WHERE id = :incident_id"
            ),
            {
                "resolved_at": RECEIVED_AT + timedelta(minutes=2),
                "incident_id": first.incident_ids[0],
            },
        )
        await session.execute(
            text(
                "UPDATE incidents SET status = 'RESOLVED', resolved_at = :resolved_at "
                "WHERE id = :incident_id"
            ),
            {
                "resolved_at": RECEIVED_AT + timedelta(minutes=3),
                "incident_id": other.incident_ids[0],
            },
        )

    reopened = await _use_case(session_factory).execute(
        SOURCE_ID,
        TOKEN_ID,
        _body(_alert("cpu", value=100)),
        RECEIVED_AT + timedelta(minutes=4),
    )

    assert reopened.incident_ids not in (first.incident_ids, other.incident_ids)
    new_incident = (
        await _rows(
            session_factory,
            f"SELECT * FROM incidents WHERE id = '{reopened.incident_ids[0]}'",
        )
    )[0]
    assert new_incident["status"] == "OPEN"
    assert new_incident["reopened_from_incident_id"] == first.incident_ids[0]
    await _assert_table_counts(
        session_factory,
        {"incidents": 3, "rca_runs": 3, "worker_jobs": 3, "outbox_events": 3},
    )


@pytest.mark.asyncio
async def test_resolved_finds_exact_identity_without_changing_human_status(
    session_factory,
):
    first = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _body(_alert("cpu")), RECEIVED_AT
    )
    other = await _use_case(session_factory).execute(
        SOURCE_ID,
        TOKEN_ID,
        _body(_alert("memory", label_overrides={"alertname": "HighMemory"})),
        RECEIVED_AT + timedelta(minutes=1),
    )

    resolved = await _use_case(session_factory).execute(
        SOURCE_ID,
        TOKEN_ID,
        _body(_alert("resolved-new-fingerprint", status="resolved", value=20)),
        RECEIVED_AT + timedelta(minutes=2),
    )

    assert resolved.incident_ids == first.incident_ids
    incidents = {
        row["id"]: row
        for row in await _rows(session_factory, "SELECT * FROM incidents")
    }
    assert incidents[first.incident_ids[0]]["alert_state"] == "RESOLVED"
    assert incidents[other.incident_ids[0]]["alert_state"] == "FIRING"
    assert incidents[first.incident_ids[0]]["status"] == "OPEN"
    assert incidents[first.incident_ids[0]]["resolved_at"] is None
    assert incidents[other.incident_ids[0]]["status"] == "OPEN"
    assert len(await _rows(session_factory, "SELECT * FROM rca_runs")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("field", CROSS_CLOUD_REQUIRED_LABELS)
@pytest.mark.parametrize("label_value", [None, "   "], ids=["missing", "blank"])
async def test_each_missing_or_blank_required_label_retains_validation_error_only(
    session_factory,
    field: str,
    label_value: str | None,
):
    alert = _alert(f"invalid-{field}-{label_value is None}")
    labels = alert["labels"]
    assert isinstance(labels, dict)
    if label_value is None:
        labels.pop(field)
    else:
        labels[field] = label_value

    result = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _body(alert), RECEIVED_AT
    )

    await _assert_validation_retention(
        session_factory,
        result,
        alert,
        [{"field": field, "code": "required"}],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cloud_provider", "azure"),
        ("severity", "urgent"),
        ("signal_type", "event"),
    ],
)
async def test_invalid_enum_retains_stable_validation_error_only(
    session_factory,
    field: str,
    value: str,
):
    alert = _alert("invalid-enum", label_overrides={field: value})

    result = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _body(alert), RECEIVED_AT
    )

    await _assert_validation_retention(
        session_factory,
        result,
        alert,
        [{"field": field, "code": "invalid_value"}],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("team", "project", "environment", "service"))
async def test_unknown_internal_scope_retains_unknown_scope_error_only(
    session_factory,
    field: str,
):
    alert = _alert("unknown-scope", label_overrides={field: f"unknown-{field}"})

    result = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _body(alert), RECEIVED_AT
    )

    await _assert_validation_retention(
        session_factory,
        result,
        alert,
        [{"field": field, "code": "unknown_scope"}],
    )


@pytest.mark.asyncio
async def test_mixed_webhook_retains_invalid_event_and_processes_valid_identity(
    session_factory,
):
    invalid = _alert("invalid")
    invalid_labels = invalid["labels"]
    assert isinstance(invalid_labels, dict)
    invalid_labels.pop("resource_id")
    valid = _alert("valid-aws", cloud_provider="aws")

    result = await _use_case(session_factory).execute(
        SOURCE_ID, TOKEN_ID, _body(invalid, valid), RECEIVED_AT
    )

    assert len(result.incident_ids) == 1
    delivery = (await _rows(session_factory, "SELECT * FROM webhook_deliveries"))[0]
    assert delivery["status"] == "VALIDATION_FAILED"
    events = {
        row["fingerprint"]: row
        for row in await _rows(session_factory, "SELECT * FROM alert_events")
    }
    assert events["invalid"]["validation_status"] == "VALIDATION_FAILED"
    assert events["invalid"]["validation_errors"] == [
        {"field": "resource_id", "code": "required"}
    ]
    assert events["valid-aws"]["validation_status"] == "VALID"
    assert events["valid-aws"]["validation_errors"] == []
    await _assert_table_counts(
        session_factory,
        {
            "ingestion_dedup_keys": 2,
            "alert_events": 2,
            "alert_instances": 1,
            "incidents": 1,
            "incident_alerts": 1,
            "rca_runs": 1,
            "worker_jobs": 1,
            "outbox_events": 1,
        },
    )


@pytest.mark.asyncio
async def test_concurrent_identical_delivery_keeps_both_deliveries_and_one_transition(
    session_factory,
):
    barrier = asyncio.Barrier(2)

    def uow_factory() -> RepositoryBarrierUnitOfWork:
        return RepositoryBarrierUnitOfWork(
            session_factory,
            dedup_barrier=barrier,
        )

    use_case = _use_case(session_factory, uow_factory=uow_factory)
    raw_body = _body(_alert("concurrent-duplicate"))

    results = await asyncio.wait_for(
        asyncio.gather(
            use_case.execute(SOURCE_ID, TOKEN_ID, raw_body, RECEIVED_AT),
            use_case.execute(
                SOURCE_ID,
                TOKEN_ID,
                raw_body,
                RECEIVED_AT + timedelta(seconds=1),
            ),
        ),
        timeout=5,
    )

    assert results[0].delivery_id != results[1].delivery_id
    reported_incidents = [
        incident_id for result in results for incident_id in result.incident_ids
    ]
    assert len(reported_incidents) == 1
    deliveries = await _rows(session_factory, "SELECT * FROM webhook_deliveries")
    assert len(deliveries) == 2
    assert Counter(row["status"] for row in deliveries) == Counter(
        {"PROCESSED": 1, "DUPLICATE": 1}
    )
    await _assert_table_counts(
        session_factory,
        {
            "ingestion_dedup_keys": 1,
            "alert_events": 1,
            "alert_instances": 1,
            "incidents": 1,
            "incident_alerts": 1,
            "rca_runs": 1,
            "worker_jobs": 1,
            "outbox_events": 1,
        },
    )


@pytest.mark.asyncio
async def test_concurrent_distinct_events_with_same_identity_share_active_incident(
    session_factory,
):
    barrier = asyncio.Barrier(2)

    def uow_factory() -> RepositoryBarrierUnitOfWork:
        return RepositoryBarrierUnitOfWork(
            session_factory,
            incident_barrier=barrier,
        )

    use_case = _use_case(session_factory, uow_factory=uow_factory)
    results = await asyncio.wait_for(
        asyncio.gather(
            use_case.execute(
                SOURCE_ID,
                TOKEN_ID,
                _body(_alert("concurrent-a", value=95)),
                RECEIVED_AT,
            ),
            use_case.execute(
                SOURCE_ID,
                TOKEN_ID,
                _body(_alert("concurrent-b", value=96)),
                RECEIVED_AT + timedelta(seconds=1),
            ),
        ),
        timeout=5,
    )

    assert len(results[0].incident_ids) == 1
    assert results[0].incident_ids == results[1].incident_ids
    deliveries = await _rows(session_factory, "SELECT * FROM webhook_deliveries")
    assert len(deliveries) == 2
    assert {row["status"] for row in deliveries} == {"PROCESSED"}
    await _assert_table_counts(
        session_factory,
        {
            "ingestion_dedup_keys": 2,
            "alert_events": 2,
            "alert_instances": 2,
            "incidents": 1,
            "incident_alerts": 2,
            "rca_runs": 1,
            "worker_jobs": 1,
            "outbox_events": 1,
        },
    )


@pytest.mark.asyncio
async def test_concurrent_create_rca_work_returns_one_run_job_and_outbox(
    session_factory,
):
    incident_id = uuid4()
    async with session_factory.begin() as session:
        await session.execute(
            text(
                """
                INSERT INTO incidents (
                    id, identity_key, title, severity, status, alert_state,
                    team_id, project_id, environment_id, service_id,
                    opened_at, created_at, updated_at
                ) VALUES (
                    :id, 'direct-job-concurrency', 'Concurrent job', 'SEV3',
                    'OPEN', 'FIRING', :team_id, :project_id, :environment_id,
                    :service_id, :opened_at, :opened_at, :opened_at
                )
                """
            ),
            {
                "id": incident_id,
                "team_id": TEAM_ID,
                "project_id": PROJECT_ID,
                "environment_id": ENVIRONMENT_ID,
                "service_id": SERVICE_ID,
                "opened_at": RECEIVED_AT,
            },
        )

    barrier = asyncio.Barrier(2)

    async def create_work() -> UUID:
        async with session_factory.begin() as session:
            return await BarrierJobRepository(session, barrier).create_rca_work(
                incident_id=incident_id,
                run_status="QUEUED",
                available_at=RECEIVED_AT,
            )

    run_ids = await asyncio.wait_for(
        asyncio.gather(create_work(), create_work()),
        timeout=5,
    )

    assert run_ids[0] == run_ids[1]
    await _assert_table_counts(
        session_factory,
        {"rca_runs": 1, "worker_jobs": 1, "outbox_events": 1},
    )
    job = (await _rows(session_factory, "SELECT * FROM worker_jobs"))[0]
    outbox = (await _rows(session_factory, "SELECT * FROM outbox_events"))[0]
    assert job["rca_run_id"] == run_ids[0]
    assert outbox["idempotency_key"] == f"rca-run:{run_ids[0]}"


@pytest.mark.asyncio
async def test_failure_after_incident_insert_rolls_back_every_ingestion_artifact(
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
