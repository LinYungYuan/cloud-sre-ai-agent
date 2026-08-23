from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from sre_agent.api.dependencies import (
    get_operator_identity_provider,
    get_operator_read_service,
)
from sre_agent.api.main import create_app
from sre_agent.application.operator.read_models import (
    OperatorIdentity,
    OperatorResourceNotFound,
    UnavailableOperatorIdentityProvider,
)

INCIDENT_ID = UUID("80000000-0000-0000-0000-000000000001")
ALERT_ID = UUID("81000000-0000-0000-0000-000000000001")
RUN_ID = UUID("82000000-0000-0000-0000-000000000001")
REPORT_ID = UUID("83000000-0000-0000-0000-000000000001")
SOURCE_ID = UUID("50000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 8, 13, 6, 30, tzinfo=UTC)


class GlobalIdentityProvider:
    async def resolve(self, authorization: str | None) -> OperatorIdentity:
        assert authorization == "Bearer operator-token"
        return OperatorIdentity("operator@example.com", global_access=True)


def _scope() -> dict[str, None]:
    return {
        "team_id": None,
        "project_id": None,
        "environment_id": None,
        "service_id": None,
    }


def _incident() -> dict[str, Any]:
    return {
        "id": INCIDENT_ID,
        "incident_number": "INC-42",
        "title": "High CPU usage",
        "severity": "SEV1",
        "status": "OPEN",
        "alert_state": "FIRING",
        "rca_status": "QUEUED",
        "provider": "AWS",
        "folder_code": "COM-LX-BOA-01",
        "alert_name": "High CPU usage",
        "scope": _scope(),
        "acknowledged": False,
        "acknowledged_at": None,
        "acknowledged_by": None,
        "assignee": None,
        "opened_at": NOW,
        "updated_at": NOW,
        "resolved_at": None,
        "version": 3,
    }


class FakeReads:
    def __init__(self) -> None:
        self.list_limits: list[int] = []
        self.trace_waterfall_run_ids: list[UUID] = []

    async def list_incidents(self, identity, *, cursor, limit):
        assert identity.global_access
        assert cursor is None
        self.list_limits.append(limit)
        return {"items": [_incident()], "next_cursor": "next-page"}

    async def get_incident(self, identity, incident_id):
        assert identity.global_access and incident_id == INCIDENT_ID
        return _incident() | {
            "description": "Grafana 告警建立的事件",
            "alert_ids": [ALERT_ID],
            "rca_run_ids": [RUN_ID],
        }

    async def get_alert(self, identity, alert_id):
        assert identity.global_access and alert_id == ALERT_ID
        return {
            "id": ALERT_ID,
            "source_id": SOURCE_ID,
            "incident_id": INCIDENT_ID,
            "fingerprint": "c6eadffa33fcdf37",
            "title": "High CPU usage",
            "severity": "SEV1",
            "state": "FIRING",
            "classification_status": "CLASSIFIED",
            "scope": _scope(),
            "starts_at": NOW,
            "ends_at": None,
            "updated_at": NOW,
            "provider": "AWS",
            "folder_code": "COM-LX-BOA-01",
            "alert_name": "High CPU usage",
            "severity_raw": "ERROR",
            "issue": {
                "rawText": "Account: 123456789012\nDB Name: production-rds-01",
                "source": "grafana.annotations.AlertValues",
                "contentType": "text/plain",
                "untrusted": True,
            },
            "normalization": {
                "status": "NORMALIZED",
                "rule_id": None,
                "rule_version": None,
            },
            "normalization_warnings": ["resource_unclassified"],
            "labels": {"alertname": "High CPU usage"},
            "annotations": {"AlertValues": "raw issue"},
            "generator_url": "https://grafana.example.com/alert/1",
        }

    async def list_rca_runs(self, identity, incident_id, *, cursor, limit):
        assert identity.global_access and incident_id == INCIDENT_ID
        assert cursor is None and limit == 50
        return {
            "items": [
                {
                    "id": RUN_ID,
                    "incident_id": INCIDENT_ID,
                    "run_number": 1,
                    "status": "QUEUED",
                    "created_at": NOW,
                    "updated_at": NOW,
                    "started_at": None,
                    "completed_at": None,
                    "failure_code": None,
                    "report_id": REPORT_ID,
                }
            ],
            "next_cursor": None,
        }

    async def get_rca_report(self, identity, rca_run_id):
        assert identity.global_access and rca_run_id == RUN_ID
        return {
            "id": REPORT_ID,
            "rca_run_id": RUN_ID,
            "incident_id": INCIDENT_ID,
            "report_version": 1,
            "status": "PARTIAL",
            "summary": "CPU 使用率過高",
            "root_cause": "尚待確認",
            "confidence": None,
            "impact": "資料庫延遲",
            "recommendations": ["確認慢查詢"],
            "hypotheses": [],
            "claims": [],
            "created_at": NOW,
        }

    async def get_trace_waterfall(self, identity, rca_run_id):
        assert identity.global_access
        self.trace_waterfall_run_ids.append(rca_run_id)
        return {
            "trace": {
                "schema_version": 1,
                "trace_id": "trace-inc-227",
                "root_service_name": "checkout-api",
                "root_operation_name": "POST /checkout",
                "started_at": NOW,
                "duration_ms": 1925.0,
                "span_count": 5,
                "representative_score": 0.96,
                "truncated": False,
                "spans": [
                    {
                        "span_id": "root",
                        "parent_span_id": None,
                        "service_name": "checkout-api",
                        "operation_name": "POST /checkout",
                        "start_offset_ms": 0.0,
                        "duration_ms": 1925.0,
                        "status": "ERROR",
                        "kind": "SERVER",
                        "critical_path": True,
                        "attributes": {"http.response.status_code": 500},
                    },
                    {
                        "span_id": "inventory-client",
                        "parent_span_id": "root",
                        "service_name": "checkout-api",
                        "operation_name": "inventory.reserve",
                        "start_offset_ms": 20.0,
                        "duration_ms": 1810.0,
                        "status": "ERROR",
                        "kind": "CLIENT",
                        "critical_path": True,
                        "attributes": {"rpc.system": "grpc"},
                    },
                    {
                        "span_id": "inventory-server",
                        "parent_span_id": "inventory-client",
                        "service_name": "inventory-service",
                        "operation_name": "inventory.reserve",
                        "start_offset_ms": 35.0,
                        "duration_ms": 1760.0,
                        "status": "ERROR",
                        "kind": "SERVER",
                        "critical_path": True,
                        "attributes": {"rpc.service": "inventory"},
                    },
                    {
                        "span_id": "db",
                        "parent_span_id": "inventory-server",
                        "service_name": "inventory-service",
                        "operation_name": "db.connection.acquire",
                        "start_offset_ms": 320.0,
                        "duration_ms": 1480.0,
                        "status": "ERROR",
                        "kind": "INTERNAL",
                        "critical_path": True,
                        "attributes": {"db.system": "postgresql"},
                    },
                    {
                        "span_id": "cache",
                        "parent_span_id": "inventory-server",
                        "service_name": "inventory-service",
                        "operation_name": "cache.lookup",
                        "start_offset_ms": 75.0,
                        "duration_ms": 120.0,
                        "status": "OK",
                        "kind": "CLIENT",
                        "critical_path": False,
                        "attributes": {"server.port": 6379},
                    },
                ],
            }
        }


@asynccontextmanager
async def _client(reads: Any, identity_provider: Any = None):
    app = create_app()
    app.dependency_overrides[get_operator_read_service] = lambda: reads
    app.dependency_overrides[get_operator_identity_provider] = lambda: (
        identity_provider or GlobalIdentityProvider()
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://operator.test",
        headers={
            "Authorization": "Bearer operator-token",
            "X-Correlation-ID": "operator-request-1",
        },
    ) as client:
        yield client


def test_operator_read_routes_are_registered() -> None:
    paths = set(create_app().openapi()["paths"])

    assert {
        "/api/v1/incidents",
        "/api/v1/incidents/{id}",
        "/api/v1/alerts/{id}",
        "/api/v1/incidents/{id}/rca-runs",
        "/api/v1/rca-runs/{id}/report",
        "/api/v1/rca-runs/{id}/trace-waterfall",
    } <= paths


@pytest.mark.asyncio
async def test_operator_reads_use_camel_case_utc_z_etag_and_bounded_pages() -> None:
    reads = FakeReads()
    async with _client(reads) as client:
        incidents = await client.get("/api/v1/incidents", params={"limit": 100})
        incident = await client.get(f"/api/v1/incidents/{INCIDENT_ID}")
        alert = await client.get(f"/api/v1/alerts/{ALERT_ID}")
        runs = await client.get(f"/api/v1/incidents/{INCIDENT_ID}/rca-runs")
        report = await client.get(f"/api/v1/rca-runs/{RUN_ID}/report")

    assert reads.list_limits == [100]
    assert incidents.status_code == 200
    assert incidents.json()["items"][0]["scope"] == {
        "teamId": None,
        "projectId": None,
        "environmentId": None,
        "serviceId": None,
    }
    assert incidents.json()["items"][0]["updatedAt"] == "2026-08-13T06:30:00Z"
    assert incidents.json()["items"][0]["provider"] == "AWS"
    assert incidents.json()["items"][0]["folderCode"] == "COM-LX-BOA-01"
    assert incidents.json()["nextCursor"] == "next-page"

    assert incident.status_code == 200
    assert incident.headers["etag"] == 'W/"3"'
    assert incident.json()["alertIds"] == [str(ALERT_ID)]
    assert set(incident.json()) == {
        "id",
        "incidentNumber",
        "title",
        "severity",
        "status",
        "alertState",
        "rcaStatus",
        "provider",
        "folderCode",
        "alertName",
        "scope",
        "acknowledged",
        "acknowledgedAt",
        "acknowledgedBy",
        "assignee",
        "openedAt",
        "updatedAt",
        "resolvedAt",
        "version",
        "description",
        "alertIds",
        "rcaRunIds",
    }

    assert alert.status_code == 200
    alert_body = alert.json()
    assert alert_body["provider"] == "AWS"
    assert alert_body["folderCode"] == "COM-LX-BOA-01"
    assert alert_body["issue"]["rawText"].startswith("Account:")
    assert alert_body["issue"]["untrusted"] is True
    assert alert_body["normalizationWarnings"] == ["resource_unclassified"]
    assert "rawBody" not in alert_body and "rawPayload" not in alert_body

    assert runs.status_code == 200
    assert runs.json()["items"][0]["runNumber"] == 1
    assert report.status_code == 200
    assert report.json()["rootCause"] == "尚待確認"
    assert report.json()["confidence"] is None
    assert report.json()["hypotheses"] == []
    for response in (incidents, incident, alert, runs, report):
        assert response.headers["x-correlation-id"] == "operator-request-1"


@pytest.mark.asyncio
async def test_trace_waterfall_route_serializes_the_safe_camel_case_projection() -> None:
    reads = FakeReads()
    async with _client(reads) as client:
        response = await client.get(f"/api/v1/rca-runs/{RUN_ID}/trace-waterfall")

    assert response.status_code == 200
    assert reads.trace_waterfall_run_ids == [RUN_ID]
    body = response.json()
    assert body["trace"]["rootServiceName"] == "checkout-api"
    assert body["trace"]["spans"][3]["criticalPath"] is True
    assert body["trace"]["spans"][4]["attributes"] == {"server.port": 6379}
    assert set(body["trace"]["spans"][0]) == {
        "spanId",
        "parentSpanId",
        "serviceName",
        "operationName",
        "startOffsetMs",
        "durationMs",
        "status",
        "kind",
        "criticalPath",
        "attributes",
    }


@pytest.mark.asyncio
async def test_trace_waterfall_route_serializes_an_absent_trace_as_null() -> None:
    class NoTraceReads(FakeReads):
        async def get_trace_waterfall(self, identity, rca_run_id):
            assert identity.global_access
            self.trace_waterfall_run_ids.append(rca_run_id)
            return {"trace": None}

    reads = NoTraceReads()
    async with _client(reads) as client:
        response = await client.get(f"/api/v1/rca-runs/{RUN_ID}/trace-waterfall")

    assert response.status_code == 200
    assert reads.trace_waterfall_run_ids == [RUN_ID]
    assert response.json() == {"trace": None}


@pytest.mark.asyncio
async def test_operator_not_found_is_an_rfc_9457_problem_without_leaking_scope() -> (
    None
):
    class MissingReads(FakeReads):
        async def get_incident(self, identity, incident_id):
            del identity, incident_id
            raise OperatorResourceNotFound

    async with _client(MissingReads()) as client:
        response = await client.get(f"/api/v1/incidents/{INCIDENT_ID}")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "urn:sre-agent:problem:resource-not-found",
        "title": "找不到資源",
        "status": 404,
        "code": "RESOURCE_NOT_FOUND",
        "detail": "資源不存在或目前的身分無權存取。",
        "correlationId": "operator-request-1",
    }


@pytest.mark.asyncio
async def test_operator_identity_fails_closed_when_not_configured() -> None:
    async with _client(FakeReads(), UnavailableOperatorIdentityProvider()) as client:
        response = await client.get("/api/v1/incidents")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == (
        "urn:sre-agent:problem:operator-identity-unavailable"
    )


@pytest.mark.asyncio
async def test_operator_limit_above_one_hundred_is_rejected() -> None:
    reads = FakeReads()
    async with _client(reads) as client:
        response = await client.get("/api/v1/incidents", params={"limit": 101})

    assert response.status_code == 400
    assert reads.list_limits == []
