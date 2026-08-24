import json
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
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
NO_TRACE_RUN_ID = UUID("9a000000-0000-0000-0000-000000000002")
REPORT_ID = UUID("9b000000-0000-0000-0000-000000000001")
TRACE_SPECIALIST_RUN_ID = UUID("9c000000-0000-0000-0000-000000000001")
AT = datetime(2026, 8, 13, 6, 30, tzinfo=UTC)

TRACE_WATERFALL = {
    "schemaVersion": 1,
    "traceId": "trace-1",
    "rootServiceName": "checkout-api",
    "rootOperationName": "POST /checkout",
    "startedAt": "2026-08-13T14:30:00+08:00",
    "durationMs": 1925.0,
    "spanCount": 5,
    "representativeScore": 0.96,
    "truncated": False,
    "spans": [
        {
            "spanId": "root",
            "parentSpanId": None,
            "serviceName": "checkout-api",
            "operationName": "POST /checkout",
            "startOffsetMs": 0.0,
            "durationMs": 1925.0,
            "status": "ERROR",
            "kind": "SERVER",
            "criticalPath": True,
            "attributes": {"http.response.status_code": 500},
        },
        {
            "spanId": "inventory-client",
            "parentSpanId": "root",
            "serviceName": "checkout-api",
            "operationName": "inventory.reserve",
            "startOffsetMs": 20.0,
            "durationMs": 1810.0,
            "status": "ERROR",
            "kind": "CLIENT",
            "criticalPath": True,
            "attributes": {"rpc.system": "grpc"},
        },
        {
            "spanId": "inventory-server",
            "parentSpanId": "inventory-client",
            "serviceName": "inventory-service",
            "operationName": "inventory.reserve",
            "startOffsetMs": 35.0,
            "durationMs": 1760.0,
            "status": "ERROR",
            "kind": "SERVER",
            "criticalPath": True,
            "attributes": {"rpc.service": "inventory"},
        },
        {
            "spanId": "db",
            "parentSpanId": "inventory-server",
            "serviceName": "inventory-service",
            "operationName": "db.connection.acquire",
            "startOffsetMs": 320.0,
            "durationMs": 1480.0,
            "status": "ERROR",
            "kind": "INTERNAL",
            "criticalPath": True,
            "attributes": {"db.system": "postgresql"},
        },
        {
            "spanId": "cache",
            "parentSpanId": "inventory-server",
            "serviceName": "inventory-service",
            "operationName": "cache.lookup",
            "startOffsetMs": 75.0,
            "durationMs": 120.0,
            "status": "OK",
            "kind": "CLIENT",
            "criticalPath": False,
            "attributes": {"server.address": "redis"},
        },
    ],
}


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
                """INSERT INTO rca_runs (
                    id, incident_id, status, created_at, updated_at
                ) VALUES ($1, $2, 'SUCCEEDED', $3, $3)""",
                (NO_TRACE_RUN_ID, OLDER_INCIDENT_ID, AT),
            ),
            (
                """INSERT INTO specialist_runs (
                    id, rca_run_id, specialist_type, status, created_at
                ) VALUES ($1, $2, 'TRACES', 'SUCCEEDED', $3)""",
                (TRACE_SPECIALIST_RUN_ID, RUN_ID, AT),
            ),
            (
                """INSERT INTO evidence_records (
                    partition_timestamp, observed_at, rca_run_id, specialist_run_id,
                    evidence_type, source_agent, source_endpoint, tool_name,
                    time_window_start, time_window_end, structured_data, raw_result,
                    metadata, content_hash
                ) VALUES (
                    $1, $1, $2, $3, 'trace.query', 'TRACE', 'trace', 'trace_query',
                    $1, $1, $4::jsonb, $5, '{}'::jsonb, 'trace-hash'
                )""",
                (
                    AT,
                    RUN_ID,
                    TRACE_SPECIALIST_RUN_ID,
                    json.dumps(TRACE_WATERFALL),
                    b'{"raw":"secret-marker"}',
                ),
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


async def _insert_trace_evidence(
    repository: SqlAlchemyOperatorReadRepository,
    structured_data: object,
    *,
    observed_at: datetime,
) -> None:
    async with repository._session_factory() as session:
        await session.execute(
            text(
                """INSERT INTO evidence_records (
                    partition_timestamp, observed_at, rca_run_id, specialist_run_id,
                    evidence_type, source_agent, source_endpoint, tool_name,
                    time_window_start, time_window_end, structured_data, raw_result,
                    metadata, content_hash
                ) VALUES (
                    :partition_timestamp, :observed_at, :rca_run_id, :specialist_run_id,
                    'trace.query', 'TRACE', 'trace', 'trace_query',
                    :partition_timestamp, :partition_timestamp,
                    CAST(:structured_data AS JSONB), :raw_result, '{}'::jsonb,
                    'newer-trace-hash'
                )"""
            ),
            {
                "partition_timestamp": AT,
                "observed_at": observed_at,
                "rca_run_id": RUN_ID,
                "specialist_run_id": TRACE_SPECIALIST_RUN_ID,
                "structured_data": json.dumps(structured_data),
                "raw_result": b'{"raw":"newer-secret-marker"}',
            },
        )


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


@pytest.mark.asyncio
async def test_trace_waterfall_projects_only_normalized_trace_data(
    repository: SqlAlchemyOperatorReadRepository,
) -> None:
    waterfall = await repository.get_trace_waterfall(
        OperatorIdentity("viewer@example.com"), RUN_ID
    )

    assert waterfall["trace"] is not None
    assert waterfall["trace"]["trace_id"] == "trace-1"
    assert waterfall["trace"]["started_at"].isoformat() == "2026-08-13T06:30:00+00:00"
    assert waterfall["trace"]["spans"][3]["operation_name"] == "db.connection.acquire"
    assert "raw_result" not in str(waterfall)
    assert "secret-marker" not in str(waterfall)


@pytest.mark.asyncio
async def test_trace_waterfall_is_null_when_an_authorized_run_has_no_trace(
    repository: SqlAlchemyOperatorReadRepository,
) -> None:
    waterfall = await repository.get_trace_waterfall(
        OperatorIdentity("viewer@example.com"), NO_TRACE_RUN_ID
    )

    assert waterfall == {"trace": None}


@pytest.mark.asyncio
async def test_trace_waterfall_does_not_fall_back_when_the_newest_trace_is_malformed(
    repository: SqlAlchemyOperatorReadRepository,
) -> None:
    await _insert_trace_evidence(
        repository,
        {"schemaVersion": 1, "traceId": "malformed"},
        observed_at=AT + timedelta(seconds=1),
    )

    waterfall = await repository.get_trace_waterfall(
        OperatorIdentity("viewer@example.com"), RUN_ID
    )

    assert waterfall == {"trace": None}


@pytest.mark.asyncio
async def test_trace_waterfall_hides_missing_and_unauthorized_runs(
    repository: SqlAlchemyOperatorReadRepository,
) -> None:
    with pytest.raises(OperatorResourceNotFound):
        await repository.get_trace_waterfall(
            OperatorIdentity("not-granted@example.com"), RUN_ID
        )
    with pytest.raises(OperatorResourceNotFound):
        await repository.get_trace_waterfall(
            OperatorIdentity("viewer@example.com"),
            UUID("9a000000-0000-0000-0000-000000000099"),
        )


@pytest.mark.asyncio
async def test_trace_waterfall_rejects_a_compact_jwt_trace_id(
    repository: SqlAlchemyOperatorReadRepository,
) -> None:
    malformed = deepcopy(TRACE_WATERFALL)
    malformed["traceId"] = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig"
    await _insert_trace_evidence(
        repository, malformed, observed_at=AT + timedelta(seconds=1)
    )

    waterfall = await repository.get_trace_waterfall(
        OperatorIdentity("viewer@example.com"), RUN_ID
    )

    assert waterfall == {"trace": None}


@pytest.mark.asyncio
async def test_trace_waterfall_rejects_compact_jwt_span_and_parent_ids(
    repository: SqlAlchemyOperatorReadRepository,
) -> None:
    malformed = deepcopy(TRACE_WATERFALL)
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig"
    malformed["spans"][0]["spanId"] = token
    malformed["spans"][1]["parentSpanId"] = token
    await _insert_trace_evidence(
        repository, malformed, observed_at=AT + timedelta(seconds=1)
    )

    waterfall = await repository.get_trace_waterfall(
        OperatorIdentity("viewer@example.com"), RUN_ID
    )

    assert waterfall == {"trace": None}


@pytest.mark.asyncio
async def test_trace_waterfall_omits_risky_attributes_and_keeps_safe_scalars(
    repository: SqlAlchemyOperatorReadRepository,
) -> None:
    normalized = deepcopy(TRACE_WATERFALL)
    normalized["spans"][0]["attributes"] = {
        "http.request.method": "POST",
        "http.response.status_code": 500,
    }
    normalized["spans"][1]["attributes"] = {"rpc.system": "not-supported"}
    normalized["spans"][2]["attributes"] = {
        "rpc.service": "inventory",
        "rpc.method": "reserve",
        "server.address": "secret-marker",
    }
    normalized["spans"][3]["attributes"] = {
        "db.system": "postgresql",
        "db.operation.name": "connection.acquire",
        "server.port": 0,
    }
    normalized["spans"][4]["attributes"] = {
        "server.address": "10.24.8.5",
        "server.port": 5432,
    }
    await _insert_trace_evidence(
        repository, normalized, observed_at=AT + timedelta(seconds=1)
    )

    waterfall = await repository.get_trace_waterfall(
        OperatorIdentity("viewer@example.com"), RUN_ID
    )

    assert waterfall["trace"] is not None
    spans = waterfall["trace"]["spans"]
    assert spans[0]["attributes"] == {
        "http.request.method": "POST",
        "http.response.status_code": 500,
    }
    assert spans[1]["attributes"] == {}
    assert spans[2]["attributes"] == {
        "rpc.service": "inventory",
        "rpc.method": "reserve",
    }
    assert spans[3]["attributes"] == {
        "db.system": "postgresql",
        "db.operation.name": "connection.acquire",
    }
    assert spans[4]["attributes"] == {
        "server.address": "10.24.8.5",
        "server.port": 5432,
    }
    assert "secret-marker" not in str(waterfall)


@pytest.mark.asyncio
@pytest.mark.parametrize("server_address", ["10.24.8.5", "2001:db8::15"])
async def test_trace_waterfall_preserves_canonical_unscoped_ip_server_addresses(
    repository: SqlAlchemyOperatorReadRepository,
    server_address: str,
) -> None:
    normalized = deepcopy(TRACE_WATERFALL)
    normalized["spans"][4]["attributes"] = {
        "server.address": server_address
    }
    await _insert_trace_evidence(
        repository, normalized, observed_at=AT + timedelta(seconds=1)
    )

    waterfall = await repository.get_trace_waterfall(
        OperatorIdentity("viewer@example.com"), RUN_ID
    )

    assert waterfall["trace"] is not None
    assert waterfall["trace"]["spans"][4]["attributes"] == {
        "server.address": server_address
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_address",
    [
        "fe80::1%ada@example.com",
        "fe80::1%authorization:secret-token",
        "fe80::1%secret-marker",
        "authorization.secret",
        "secret-marker.internal",
        "raw.payload",
    ],
)
async def test_trace_waterfall_omits_scoped_ips_and_hostnames(
    repository: SqlAlchemyOperatorReadRepository,
    unsafe_address: str,
) -> None:
    normalized = deepcopy(TRACE_WATERFALL)
    normalized["spans"][4]["attributes"] = {
        "server.address": unsafe_address
    }
    await _insert_trace_evidence(
        repository, normalized, observed_at=AT + timedelta(seconds=1)
    )

    waterfall = await repository.get_trace_waterfall(
        OperatorIdentity("viewer@example.com"), RUN_ID
    )

    assert waterfall["trace"] is not None
    assert waterfall["trace"]["spans"][4]["attributes"] == {}
    assert unsafe_address not in str(waterfall)
