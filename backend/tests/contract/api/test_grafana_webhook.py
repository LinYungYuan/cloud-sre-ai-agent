import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from sre_agent.api.dependencies import (
    get_grafana_authenticator,
    get_ingest_grafana_alerts,
)
from sre_agent.api.main import create_app
from sre_agent.application.alerts.ingest_grafana_alerts import IngestionResult
from sre_agent.integrations.grafana.authenticator import GrafanaUnauthorized
from sre_agent.integrations.grafana.payloads import (
    GrafanaPayloadInvalid,
    GrafanaPayloadTooLarge,
)

SOURCE_ID = UUID("50000000-0000-0000-0000-000000000001")
DELIVERY_ID = UUID("60000000-0000-0000-0000-000000000001")
ACCEPTED_AT = datetime(2026, 8, 12, 2, 0, 1, tzinfo=UTC)
VALID_BODY = json.dumps(
    {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {},
                "annotations": {},
                "startsAt": "2026-08-12T02:00:00Z",
                "endsAt": "2026-08-12T03:00:00Z",
                "values": {},
                "generatorURL": "https://grafana.example.com/alert/1",
                "fingerprint": "contract-test",
            }
        ],
    },
    separators=(",", ":"),
).encode()


class RecordingAuthenticator:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[UUID, str | None]] = []

    def verify(self, source_id: UUID, authorization: str | None) -> str:
        self.calls.append((source_id, authorization))
        if self.error is not None:
            raise self.error
        return "current-2026-08"


class RecordingIngestion:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[UUID, str, bytes, datetime]] = []

    async def execute(
        self,
        source_id: UUID,
        token_id: str,
        raw_body: bytes,
        received_at: datetime,
    ) -> IngestionResult:
        self.calls.append((source_id, token_id, raw_body, received_at))
        if self.error is not None:
            raise self.error
        return IngestionResult(
            delivery_id=DELIVERY_ID,
            accepted_at=ACCEPTED_AT,
            incident_ids=(),
        )


@asynccontextmanager
async def _client(
    authenticator: RecordingAuthenticator,
    ingestion: RecordingIngestion,
) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI]]:
    app = create_app()
    app.dependency_overrides[get_grafana_authenticator] = lambda: authenticator
    app.dependency_overrides[get_ingest_grafana_alerts] = lambda: ingestion
    transport = httpx.ASGITransport(
        app=app,  # pyright: ignore[reportArgumentType] -- httpx/FastAPI ASGI stubs differ
        raise_app_exceptions=False,
    )
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://contract.test"
        ) as client:
            yield client, app
    finally:
        app.dependency_overrides.clear()


def _headers(correlation_id: str | None = "request-123") -> dict[str, str]:
    headers = {
        "Authorization": "Bearer accepted-token",
        "Content-Type": "application/json",
    }
    if correlation_id is not None:
        headers["X-Correlation-ID"] = correlation_id
    return headers


def _assert_problem(
    response: httpx.Response,
    *,
    status: int,
    title: str,
) -> dict[str, Any]:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    problem = response.json()
    assert problem["status"] == status
    assert problem["title"] == title
    assert isinstance(problem["type"], str) and problem["type"]
    assert problem["correlationId"] == response.headers["x-correlation-id"]
    return problem


@pytest.mark.asyncio
async def test_accepts_json_and_returns_only_the_contract_response_fields() -> None:
    authenticator = RecordingAuthenticator()
    ingestion = RecordingIngestion()

    async with _client(authenticator, ingestion) as (client, app):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers(),
        )

    assert response.status_code == 202
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-correlation-id"] == "request-123"
    assert response.json() == {
        "deliveryId": str(DELIVERY_ID),
        "acceptedAt": "2026-08-12T02:00:01Z",
    }
    assert authenticator.calls == [(SOURCE_ID, "Bearer accepted-token")]
    assert len(ingestion.calls) == 1
    source_id, token_id, raw_body, received_at = ingestion.calls[0]
    assert source_id == SOURCE_ID
    assert token_id == "current-2026-08"
    assert raw_body == VALID_BODY
    assert received_at.tzinfo is UTC
    assert app.dependency_overrides == {}


@pytest.mark.asyncio
async def test_accepts_application_json_with_media_type_parameters() -> None:
    authenticator = RecordingAuthenticator()
    ingestion = RecordingIngestion()
    headers = _headers()
    headers["Content-Type"] = "Application/JSON; charset=UTF-8"

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=headers,
        )

    assert response.status_code == 202


@pytest.mark.asyncio
async def test_rejects_a_non_json_content_type_without_calling_ingestion() -> None:
    authenticator = RecordingAuthenticator()
    ingestion = RecordingIngestion()
    headers = _headers()
    headers["Content-Type"] = "text/plain"

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=headers,
        )

    _assert_problem(response, status=400, title="Invalid request")
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_authentication_failure_never_calls_ingestion_or_leaks_credentials() -> (
    None
):
    supplied_credential = "do-not-reflect-this-token"
    authenticator = RecordingAuthenticator(GrafanaUnauthorized("sensitive auth error"))
    ingestion = RecordingIngestion()
    headers = _headers()
    headers["Authorization"] = f"Bearer {supplied_credential}"

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=headers,
        )

    problem = _assert_problem(response, status=401, title="Unauthorized")
    assert ingestion.calls == []
    assert supplied_credential not in response.text
    assert "sensitive auth error" not in problem.get("detail", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "title"),
    [
        (GrafanaPayloadInvalid("sensitive payload"), 400, "Invalid request"),
        (GrafanaPayloadTooLarge("sensitive payload"), 413, "Payload too large"),
    ],
)
async def test_maps_expected_ingestion_errors_to_problem_json(
    error: Exception,
    status: int,
    title: str,
) -> None:
    authenticator = RecordingAuthenticator()
    ingestion = RecordingIngestion(error)

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers(),
        )

    problem = _assert_problem(response, status=status, title=title)
    assert "sensitive payload" not in problem.get("detail", "")


@pytest.mark.asyncio
async def test_invalid_source_uuid_is_a_400_problem_and_skips_dependencies() -> None:
    authenticator = RecordingAuthenticator()
    ingestion = RecordingIngestion()

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            "/webhooks/v1/grafana/not-a-uuid",
            content=VALID_BODY,
            headers=_headers(),
        )

    _assert_problem(response, status=400, title="Invalid request")
    assert authenticator.calls == []
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_unexpected_errors_return_a_generic_500_with_correlation_id() -> None:
    sensitive_exception = RuntimeError(
        'database failed for token=do-not-log body={"private":"value"}'
    )
    authenticator = RecordingAuthenticator()
    ingestion = RecordingIngestion(sensitive_exception)

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers("trace-500"),
        )

    problem = _assert_problem(response, status=500, title="Internal server error")
    assert problem["correlationId"] == "trace-500"
    assert problem["detail"] == "An unexpected error occurred."
    assert "do-not-log" not in response.text
    assert "private" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "supplied",
    ["contains spaces", "line\r\nbreak", "x" * 129],
)
async def test_unsafe_correlation_ids_are_replaced_with_safe_generated_values(
    supplied: str,
) -> None:
    authenticator = RecordingAuthenticator()
    ingestion = RecordingIngestion()

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers(supplied),
        )

    correlation_id = response.headers["x-correlation-id"]
    assert correlation_id != supplied
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", correlation_id)
    assert "\r" not in correlation_id and "\n" not in correlation_id


@pytest.mark.asyncio
async def test_non_ascii_correlation_id_bytes_are_not_reflected() -> None:
    authenticator = RecordingAuthenticator()
    ingestion = RecordingIngestion()
    headers = [
        (b"authorization", b"Bearer accepted-token"),
        (b"content-type", b"application/json"),
        (b"x-correlation-id", b"non-ascii-\xff"),
    ]

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=headers,
        )

    correlation_id = response.headers["x-correlation-id"]
    assert correlation_id != "non-ascii-ÿ"
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", correlation_id)


@pytest.mark.asyncio
async def test_missing_correlation_id_is_generated_and_echoed() -> None:
    authenticator = RecordingAuthenticator()
    ingestion = RecordingIngestion()

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers(None),
        )

    assert re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
        response.headers["x-correlation-id"],
    )


@pytest.mark.asyncio
async def test_thin_http_boundary_completes_under_two_seconds_with_fakes() -> None:
    authenticator = RecordingAuthenticator()
    ingestion = RecordingIngestion()

    started_at = perf_counter()
    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers(),
        )
    elapsed = perf_counter() - started_at

    assert response.status_code == 202
    assert elapsed < 2.0
