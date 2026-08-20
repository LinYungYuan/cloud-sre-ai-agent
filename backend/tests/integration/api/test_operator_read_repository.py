import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sre_agent.application.operator.read_models import (
    OperatorCursorInvalid,
    OperatorIdentity,
    OperatorResourceNotFound,
)
from sre_agent.persistence.repositories.operator_reads import (
    SqlAlchemyOperatorReadRepository,
)

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
)
TEAM_ID = UUID("91000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("92000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("93000000-0000-0000-0000-000000000001")
SOURCE_ID = UUID("94000000-0000-0000-0000-000000000001")
SUBJECT_ID = UUID("95000000-0000-0000-0000-000000000001")
DELIVERY_ID = UUID("96000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("97000000-0000-0000-0000-000000000001")
LATEST_EVENT_ID = UUID("97000000-0000-0000-0000-000000000002")
INSTANCE_ID = UUID("98000000-0000-0000-0000-000000000001")
INCIDENT_ID = UUID("99000000-0000-0000-0000-000000000001")
OLDER_INCIDENT_ID = UUID("99000000-0000-0000-0000-000000000002")
RUN_ID = UUID("9a000000-0000-0000-0000-000000000001")
REPORT_ID = UUID("9b000000-0000-0000-0000-000000000001")
AT = datetime(2026, 8, 13, 6, 30, tzinfo=UTC)


@pytest_asyncio.fixture
async def repository():
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        statements = (
            (
                "INSERT INTO teams (id, name) VALUES ($1, 'operator-team')",
                (TEAM_ID,),
            ),
            (
                "INSERT INTO projects (id, team_id, name) VALUES ($1, $2, 'operator-project')",
                (PROJECT_ID, TEAM_ID),
            ),
            (
                "INSERT INTO environments (id, project_id, name) VALUES ($1, $2, 'operator-env')",
                (ENVIRONMENT_ID, PROJECT_ID),
            ),
            (
                "INSERT INTO grafana_sources (id, project_id, environment_id, name) VALUES ($1, $2, $3, 'operator-source')",
                (SOURCE_ID, PROJECT_ID, ENVIRONMENT_ID),
            ),
            (
                "INSERT INTO subjects (id, external_id, subject_type, display_name) VALUES ($1, 'viewer@example.com', 'USER', '值班人員')",
                (SUBJECT_ID,),
            ),
            (
                "INSERT INTO scope_grants (subject_id, project_id, role) VALUES ($1, $2, 'VIEWER')",
                (SUBJECT_ID, PROJECT_ID),
            ),
            (
                """INSERT INTO webhook_deliveries (
                    id, partition_timestamp, received_at, source_id, token_id,
                    body_hash, raw_body, raw_payload, status
                ) VALUES ($1, $2, $2, $3, 'operator-token', 'hash', $4, '{}'::jsonb, 'PROCESSED')""",
                (DELIVERY_ID, AT, SOURCE_ID, b"{}"),
            ),
            (
                """INSERT INTO alert_events (
                    id, partition_timestamp, observed_at, source_id, delivery_id,
                    delivery_partition_timestamp, fingerprint, alert_state,
                    validation_status, starts_at, labels, annotations, raw_payload,
                    provider, folder_code, alert_name, severity_raw,
                    severity_canonical, issue, normalization_status,
                    normalization_warnings
                ) VALUES (
                    $1, $2, $2, $3, $4, $2, 'fingerprint-1', 'FIRING', 'VALID',
                    $2, '{"alertname":"High CPU usage"}'::jsonb,
                    '{"AlertValues":"CPU is high"}'::jsonb,
                    '{"generatorURL":"https://grafana.example.com/alert/1"}'::jsonb,
                    'AWS', 'COM-LX-BOA-01', 'High CPU usage', 'ERROR', 'SEV1',
                    '{"rawText":"CPU is high","source":"grafana.annotations.AlertValues","contentType":"text/plain","untrusted":true}'::jsonb,
                    'NORMALIZED', '[]'::jsonb
                )""",
                (EVENT_ID, AT, SOURCE_ID, DELIVERY_ID),
            ),
            (
                """INSERT INTO alert_instances (
                    id, source_id, fingerprint, latest_event_id,
                    latest_event_partition_timestamp, state, labels, annotations,
                    first_seen_at, last_seen_at
                ) VALUES (
                    $1, $2, 'fingerprint-1', $3, $4, 'FIRING', '{}'::jsonb,
                    '{}'::jsonb, $4, $4
                )""",
                (INSTANCE_ID, SOURCE_ID, EVENT_ID, AT),
            ),
            (
                """INSERT INTO incidents (
                    id, identity_key, identity_version, title, severity, status,
                    alert_state, provider, folder_code, alert_name, team_id,
                    project_id, environment_id, opened_at, created_at, updated_at
                ) VALUES (
                    $1, 'operator-identity', 2, 'High CPU usage', 'SEV1', 'OPEN',
                    'FIRING', 'AWS', 'COM-LX-BOA-01', 'High CPU usage', $2, $3,
                    $4, $5, $5, $5
                )""",
                (INCIDENT_ID, TEAM_ID, PROJECT_ID, ENVIRONMENT_ID, AT),
            ),
            (
                """INSERT INTO incidents (
                    id, identity_key, identity_version, title, severity, status,
                    alert_state, provider, folder_code, alert_name, team_id,
                    project_id, environment_id, opened_at, created_at, updated_at
                ) VALUES (
                    $1, 'older-operator-identity', 2, 'Older alert', 'SEV3', 'RESOLVED',
                    'RESOLVED', 'AWS', 'COM-LX-BOA-01', 'Older alert', $2, $3,
                    $4, $5, $5, $5
                )""",
                (
                    OLDER_INCIDENT_ID,
                    TEAM_ID,
                    PROJECT_ID,
                    ENVIRONMENT_ID,
                    AT - timedelta(minutes=1),
                ),
            ),
            (
                """INSERT INTO incident_alerts (
                    incident_id, alert_event_id, alert_event_partition_timestamp
                ) VALUES ($1, $2, $3)""",
                (INCIDENT_ID, EVENT_ID, AT),
            ),
            (
                """INSERT INTO alert_events (
                    id, partition_timestamp, observed_at, source_id, delivery_id,
                    delivery_partition_timestamp, fingerprint, alert_state,
                    validation_status, starts_at, labels, annotations, raw_payload,
                    provider, folder_code, alert_name, severity_raw,
                    severity_canonical, issue, normalization_status,
                    normalization_warnings
                ) VALUES (
                    $1, $2, $2, $3, $4, $5, 'fingerprint-1', 'FIRING', 'VALID',
                    $2, '{"alertname":"High CPU usage"}'::jsonb,
                    '{"AlertValues":"CPU remains high"}'::jsonb,
                    '{"generatorURL":"https://grafana.example.com/alert/1"}'::jsonb,
                    'AWS', 'COM-LX-BOA-01', 'High CPU usage', 'ERROR', 'SEV1',
                    '{"rawText":"CPU remains high","source":"grafana.annotations.AlertValues","contentType":"text/plain","untrusted":true}'::jsonb,
                    'NORMALIZED', '[]'::jsonb
                )""",
                (
                    LATEST_EVENT_ID,
                    AT + timedelta(seconds=1),
                    SOURCE_ID,
                    DELIVERY_ID,
                    AT,
                ),
            ),
            (
                """UPDATE alert_instances
                    SET latest_event_id = $1,
                        latest_event_partition_timestamp = $2,
                        last_seen_at = $2
                    WHERE id = $3""",
                (LATEST_EVENT_ID, AT + timedelta(seconds=1), INSTANCE_ID),
            ),
            (
                """INSERT INTO rca_runs (
                    id, incident_id, status, created_at, updated_at
                ) VALUES ($1, $2, 'QUEUED', $3, $3)""",
                (RUN_ID, INCIDENT_ID, AT),
            ),
            (
                """INSERT INTO rca_reports (
                    id, rca_run_id, version, summary, report, created_at,
                    result_status
                ) VALUES (
                    $1, $2, 1, 'CPU 使用率過高',
                    '{"status":"PARTIAL","rootCause":"尚待確認","confidence":0.42,"impact":"資料庫延遲","recommendations":["確認慢查詢"],"hypotheses":[{"statement":"資料庫負載增加","confidence":0.42,"claims":[]}],"claims":[]}'::jsonb,
                    $3, 'PARTIAL'
                )""",
                (REPORT_ID, RUN_ID, AT),
            ),
        )
        for sql, parameters in statements:
            await connection.exec_driver_sql(sql, parameters)
        factory = async_sessionmaker(bind=connection, expire_on_commit=False)
        try:
            yield SqlAlchemyOperatorReadRepository(factory)
        finally:
            await transaction.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_scoped_operator_reads_normalized_incident_alert_run_and_report(
    repository: SqlAlchemyOperatorReadRepository,
) -> None:
    identity = OperatorIdentity("viewer@example.com")

    page = await repository.list_incidents(identity, cursor=None, limit=100)
    incident = await repository.get_incident(identity, INCIDENT_ID)
    alert = await repository.get_alert(identity, INSTANCE_ID)
    runs = await repository.list_rca_runs(identity, INCIDENT_ID, cursor=None, limit=100)
    report = await repository.get_rca_report(identity, RUN_ID)

    assert len(page["items"]) == 2
    assert page["next_cursor"] is None
    assert incident["alert_ids"] == [INSTANCE_ID]
    assert incident["rca_run_ids"] == [RUN_ID]
    assert alert["provider"] == "AWS"
    assert alert["folder_code"] == "COM-LX-BOA-01"
    assert alert["issue"]["rawText"] == "CPU remains high"
    assert "raw_body" not in alert and "raw_payload" not in alert
    assert runs["items"][0]["run_number"] == 1
    assert report["root_cause"] == "尚待確認"
    assert report["confidence"] == pytest.approx(0.42)
    assert report["hypotheses"][0]["statement"] == "資料庫負載增加"


@pytest.mark.asyncio
async def test_incident_cursor_uses_the_sort_tuple_without_skips(
    repository: SqlAlchemyOperatorReadRepository,
) -> None:
    identity = OperatorIdentity("viewer@example.com")

    first = await repository.list_incidents(identity, cursor=None, limit=1)
    second = await repository.list_incidents(
        identity,
        cursor=first["next_cursor"],
        limit=1,
    )

    assert [item["id"] for item in first["items"]] == [INCIDENT_ID]
    assert first["next_cursor"] is not None
    assert [item["id"] for item in second["items"]] == [OLDER_INCIDENT_ID]
    assert second["next_cursor"] is None


@pytest.mark.asyncio
async def test_unauthorized_and_missing_incidents_are_indistinguishable(
    repository: SqlAlchemyOperatorReadRepository,
) -> None:
    identity = OperatorIdentity("not-granted@example.com")

    assert await repository.list_incidents(identity, cursor=None, limit=100) == {
        "items": [],
        "next_cursor": None,
    }
    with pytest.raises(OperatorResourceNotFound):
        await repository.get_incident(identity, INCIDENT_ID)
    with pytest.raises(OperatorResourceNotFound):
        await repository.get_incident(
            OperatorIdentity("viewer@example.com"),
            UUID("99000000-0000-0000-0000-000000000099"),
        )


@pytest.mark.asyncio
async def test_invalid_cursor_is_rejected_without_querying_an_unbounded_page(
    repository: SqlAlchemyOperatorReadRepository,
) -> None:
    with pytest.raises(OperatorCursorInvalid):
        await repository.list_incidents(
            OperatorIdentity("viewer@example.com"),
            cursor="not-a-valid-cursor",
            limit=100,
        )
