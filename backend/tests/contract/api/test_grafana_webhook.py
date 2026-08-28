import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import perf_counter
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from sre_agent.api.dependencies import (
    get_grafana_authenticator,
    get_ingest_grafana_alerts,
)
from sre_agent.api.main import create_app
from sre_agent.application.alerts.ingest_grafana_alerts import (
    IngestGrafanaAlerts,
    IngestionResult,
    StaticClassifierProvider,
)
from sre_agent.application.outbox.publish_events import (
    OutboxPublishResult,
    PublishResultCode,
)
from sre_agent.domain.alerts.classification import ClassificationResult
from sre_agent.integrations.grafana.authenticator import GrafanaUnauthorized
from sre_agent.integrations.grafana.payloads import (
    GrafanaPayloadInvalid,
    GrafanaPayloadTooLarge,
)
from sre_agent.persistence.repositories.alerts import (
    AlertRepository,
    SourceScope,
    StoredAlertEvent,
)
from sre_agent.persistence.repositories.incidents import (
    IncidentRepository,
    IncidentSelection,
)
from sre_agent.persistence.repositories.jobs import JobRepository, RcaWorkCreation

SOURCE_ID = UUID("50000000-0000-0000-0000-000000000001")
DELIVERY_ID = UUID("60000000-0000-0000-0000-000000000001")
ACCEPTED_AT = datetime(2026, 8, 12, 2, 0, 1, tzinfo=UTC)
TEAM_ID = UUID("10000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("20000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
INCIDENT_ID = UUID("80000000-0000-0000-0000-000000000001")
OUTBOX_EVENT_ID = UUID("90000000-0000-0000-0000-000000000001")
REDELIVERY_ID = UUID("60000000-0000-0000-0000-000000000002")
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
MAX_BODY_BYTES = 1_048_576


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


class FakeAlertRepository:
    def __init__(self) -> None:
        self.delivery_finished = False

    async def get_source_scope(self, source_id: UUID) -> SourceScope:
        assert source_id == SOURCE_ID
        return SourceScope(TEAM_ID, PROJECT_ID, ENVIRONMENT_ID)

    async def create_delivery(self, **values: Any) -> UUID:
        assert values["source_id"] == SOURCE_ID
        assert values["token_id"] == "current-2026-08"
        return DELIVERY_ID

    async def claim_dedup_key(self, **values: Any) -> bool:
        assert values["delivery_id"] == DELIVERY_ID
        return False

    async def add_event(self, **values: Any) -> StoredAlertEvent:
        assert values["validation_status"] == "VALIDATION_FAILED"
        return StoredAlertEvent(
            id=UUID("70000000-0000-0000-0000-000000000001"),
        )

    async def upsert_instance(self, **values: Any) -> None:
        raise AssertionError(
            f"invalid contract payload must not create an instance: {values}"
        )

    async def finish_delivery(self, **values: Any) -> None:
        assert values["status"] == "DUPLICATE"
        self.delivery_finished = True


class UnusedRepository:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"invalid contract payload must not call {name}")


class ObservableFakeUnitOfWork:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.alert_repository = FakeAlertRepository()
        self.alerts: AlertRepository = cast(AlertRepository, self.alert_repository)
        self.incidents: IncidentRepository = cast(
            IncidentRepository, UnusedRepository()
        )
        self.jobs: JobRepository = cast(JobRepository, UnusedRepository())
        self.fail_commit = fail_commit
        self.commit_started = False
        self.commit_completed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        if exc_type is not None:
            return
        assert self.alert_repository.delivery_finished
        self.commit_started = True
        await asyncio.sleep(0.05)
        if self.fail_commit:
            raise RuntimeError("fake commit failed with sensitive state")
        self.commit_completed = True


class NeverClassifier:
    def classify(
        self,
        labels: Mapping[str, object],
        rule_uid: str | None,
        folder: str | None,
    ) -> ClassificationResult:
        raise AssertionError(
            f"invalid contract labels must not be classified: {labels}, "
            f"{rule_uid}, {folder}"
        )


class NewIncidentAlertRepository(FakeAlertRepository):
    async def claim_dedup_key(self, **values: Any) -> bool:
        assert values["delivery_id"] == DELIVERY_ID
        return True

    async def add_event(self, **values: Any) -> StoredAlertEvent:
        return StoredAlertEvent(
            id=UUID("70000000-0000-0000-0000-000000000001"),
        )

    async def upsert_instance(self, **values: Any) -> None:
        del values

    async def finish_delivery(self, **values: Any) -> None:
        assert values["status"] == "VALIDATION_FAILED"
        self.delivery_finished = True


class NewIncidentRepository:
    async def latest_resolved(self, *args: Any, **values: Any) -> None:
        del args, values

    async def get_or_create_active(self, **values: Any) -> IncidentSelection:
        assert values["opened_at"].tzinfo is UTC
        return IncidentSelection(INCIDENT_ID, created=True)

    async def link_alert(self, *args: Any, **values: Any) -> None:
        del args, values

    async def set_alert_state(self, *args: Any, **values: Any) -> None:
        del args, values


class NewIncidentJobRepository:
    async def create_rca_work(self, **values: Any) -> RcaWorkCreation:
        assert values["incident_id"] == INCIDENT_ID
        return RcaWorkCreation(INCIDENT_ID, OUTBOX_EVENT_ID)


class CommitAwareUnitOfWork(ObservableFakeUnitOfWork):
    def __init__(self, *, fail_commit: bool = False) -> None:
        super().__init__(fail_commit=fail_commit)
        self.alert_repository = NewIncidentAlertRepository()
        self.alerts = cast(AlertRepository, self.alert_repository)
        self.incidents = cast(IncidentRepository, NewIncidentRepository())
        self.jobs = cast(JobRepository, NewIncidentJobRepository())


class CommitAwarePublisher:
    def __init__(self, uow: CommitAwareUnitOfWork, *, fail: bool = False) -> None:
        self._uow = uow
        self._fail = fail
        self.event_ids: list[UUID] = []

    async def publish_event(self, event_id: UUID) -> OutboxPublishResult:
        assert self._uow.commit_completed
        self.event_ids.append(event_id)
        if self._fail:
            raise RuntimeError("publisher unavailable")
        return OutboxPublishResult(event_id, "PENDING", PublishResultCode.PUBLISHED)


class CrashWindowState:
    def __init__(self) -> None:
        self.dedup_committed = False
        self.interrupt_once = True
        self.delivery_count = 0
        self.alert_event_count = 0
        self.rca_run_count = 0
        self.worker_job_count = 0
        self.outbox_event_count = 0
        self.delivery_statuses: list[str] = []


class CrashWindowAlertRepository:
    def __init__(self, state: CrashWindowState) -> None:
        self._state = state
        self.delivery_finished = False

    async def create_delivery(self, **values: Any) -> UUID:
        assert values["source_id"] == SOURCE_ID
        assert values["token_id"] == "current-2026-08"
        delivery_id = DELIVERY_ID if self._state.delivery_count == 0 else REDELIVERY_ID
        self._state.delivery_count += 1
        return delivery_id

    async def claim_dedup_key(self, **values: Any) -> bool:
        assert values["source_id"] == SOURCE_ID
        return not self._state.dedup_committed

    async def add_event(self, **values: Any) -> StoredAlertEvent:
        self._state.alert_event_count += 1
        return StoredAlertEvent(
            id=UUID("70000000-0000-0000-0000-000000000001"),
        )

    async def upsert_instance(self, **values: Any) -> None:
        del values

    async def finish_delivery(self, **values: Any) -> None:
        self.delivery_finished = True
        self._state.delivery_statuses.append(values["status"])


class CrashWindowIncidentRepository:
    async def latest_resolved(self, *args: Any, **values: Any) -> None:
        del args, values

    async def get_or_create_active(self, **values: Any) -> IncidentSelection:
        assert values["opened_at"].tzinfo is UTC
        return IncidentSelection(INCIDENT_ID, created=True)

    async def link_alert(self, *args: Any, **values: Any) -> None:
        del args, values

    async def set_alert_state(self, *args: Any, **values: Any) -> None:
        del args, values


class CrashWindowJobRepository:
    def __init__(self, state: CrashWindowState) -> None:
        self._state = state

    async def create_rca_work(self, **values: Any) -> RcaWorkCreation:
        assert values["incident_id"] == INCIDENT_ID
        self._state.rca_run_count += 1
        self._state.worker_job_count += 1
        self._state.outbox_event_count += 1
        return RcaWorkCreation(INCIDENT_ID, OUTBOX_EVENT_ID)


class CrashWindowUnitOfWork:
    def __init__(self, state: CrashWindowState) -> None:
        self._state = state
        self.alert_repository = CrashWindowAlertRepository(state)
        self.alerts = cast(AlertRepository, self.alert_repository)
        self.incidents = cast(IncidentRepository, CrashWindowIncidentRepository())
        self.jobs = cast(JobRepository, CrashWindowJobRepository(state))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        if exc_type is not None:
            return
        assert self.alert_repository.delivery_finished
        self._state.dedup_committed = True
        if self._state.interrupt_once:
            self._state.interrupt_once = False
            raise RuntimeError("simulated response interruption after commit")


class CrashWindowPublisher:
    def __init__(self) -> None:
        self.event_ids: list[UUID] = []

    async def publish_event(self, event_id: UUID) -> OutboxPublishResult:
        self.event_ids.append(event_id)
        raise AssertionError(f"unexpected publish during crash-window test: {event_id}")


def _real_ingestion(
    uow: ObservableFakeUnitOfWork,
    publisher: CommitAwarePublisher | None = None,
) -> IngestGrafanaAlerts:
    return IngestGrafanaAlerts(
        uow_factory=lambda: uow,
        classifier_provider=StaticClassifierProvider(NeverClassifier()),
        max_body_bytes=1_048_576,
        outbox_publish_service=publisher,
    )


@asynccontextmanager
async def _client(
    authenticator: RecordingAuthenticator,
    ingestion: Any,
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
async def test_authentication_failure_does_not_consume_request_body() -> None:
    authenticator = RecordingAuthenticator(GrafanaUnauthorized("invalid"))
    ingestion = RecordingIngestion()
    receive_count = 0

    async def body_that_must_not_be_received() -> AsyncIterator[bytes]:
        nonlocal receive_count
        receive_count += 1
        raise AssertionError("unauthorized request body was consumed")
        yield b"unreachable"

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=body_that_must_not_be_received(),
            headers=_headers(),
        )

    _assert_problem(response, status=401, title="Unauthorized")
    assert receive_count == 0
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_authenticated_body_one_byte_over_limit_stops_receiving_immediately() -> (
    None
):
    authenticator = RecordingAuthenticator()
    ingestion = RecordingIngestion()
    yielded_chunks = 0

    async def oversized_body() -> AsyncIterator[bytes]:
        nonlocal yielded_chunks
        for chunk in (b"x" * MAX_BODY_BYTES, b"y"):
            yielded_chunks += 1
            yield chunk
        raise AssertionError("receiver read beyond max_bytes + 1")

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=oversized_body(),
            headers=_headers(),
        )

    _assert_problem(response, status=413, title="Payload too large")
    assert yielded_chunks == 2
    assert ingestion.calls == []


@pytest.mark.asyncio
async def test_authenticated_body_at_exact_limit_is_accepted() -> None:
    authenticator = RecordingAuthenticator()
    ingestion = RecordingIngestion()
    exact_body = b"x" * MAX_BODY_BYTES

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=exact_body,
            headers=_headers(),
        )

    assert response.status_code == 202
    assert ingestion.calls[0][2] == exact_body


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
    uow = ObservableFakeUnitOfWork()
    ingestion = _real_ingestion(uow)

    started_at = perf_counter()
    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers(),
        )
    elapsed = perf_counter() - started_at

    assert response.status_code == 202
    assert response.json()["deliveryId"] == str(DELIVERY_ID)
    assert uow.commit_started
    assert uow.commit_completed
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_webhook_publishes_new_event_only_after_commit() -> None:
    authenticator = RecordingAuthenticator()
    uow = CommitAwareUnitOfWork()
    publisher = CommitAwarePublisher(uow)
    ingestion = _real_ingestion(uow, publisher)

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers("post-commit-publish"),
        )

    assert response.status_code == 202
    assert publisher.event_ids == [OUTBOX_EVENT_ID]


@pytest.mark.asyncio
async def test_publish_failure_still_returns_accepted_response() -> None:
    authenticator = RecordingAuthenticator()
    uow = CommitAwareUnitOfWork()
    publisher = CommitAwarePublisher(uow, fail=True)
    ingestion = _real_ingestion(uow, publisher)

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers("publish-failure"),
        )

    assert response.status_code == 202
    assert publisher.event_ids == [OUTBOX_EVENT_ID]


@pytest.mark.asyncio
async def test_commit_failure_never_returns_an_accepted_response() -> None:
    authenticator = RecordingAuthenticator()
    uow = ObservableFakeUnitOfWork(fail_commit=True)
    ingestion = _real_ingestion(uow)

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers("commit-failure"),
        )

    problem = _assert_problem(response, status=500, title="Internal server error")
    assert uow.commit_started
    assert not uow.commit_completed
    assert problem["detail"] == "An unexpected error occurred."
    assert "sensitive state" not in response.text


@pytest.mark.asyncio
async def test_commit_failure_never_attempts_new_event_publish() -> None:
    authenticator = RecordingAuthenticator()
    uow = CommitAwareUnitOfWork(fail_commit=True)
    publisher = CommitAwarePublisher(uow)
    ingestion = _real_ingestion(uow, publisher)

    async with _client(authenticator, ingestion) as (client, _):
        response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers("commit-failure-no-publish"),
        )

    _assert_problem(response, status=500, title="Internal server error")
    assert publisher.event_ids == []


@pytest.mark.asyncio
async def test_redelivery_after_commit_before_response_does_not_create_second_rca_work() -> (
    None
):
    authenticator = RecordingAuthenticator()
    state = CrashWindowState()
    publisher = CrashWindowPublisher()
    ingestion = IngestGrafanaAlerts(
        uow_factory=lambda: CrashWindowUnitOfWork(state),
        classifier_provider=StaticClassifierProvider(NeverClassifier()),
        max_body_bytes=1_048_576,
        outbox_publish_service=publisher,
    )

    async with _client(authenticator, ingestion) as (client, _):
        first_response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers("crash-window-first"),
        )
        second_response = await client.post(
            f"/webhooks/v1/grafana/{SOURCE_ID}",
            content=VALID_BODY,
            headers=_headers("crash-window-redelivery"),
        )

    _assert_problem(first_response, status=500, title="Internal server error")
    assert second_response.status_code == 202
    assert state.delivery_count == 2
    assert state.delivery_statuses == ["VALIDATION_FAILED", "DUPLICATE"]
    assert state.alert_event_count == 1
    assert state.rca_run_count == 1
    assert state.worker_job_count == 1
    assert state.outbox_event_count == 1
    assert publisher.event_ids == []
