from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest

from sre_agent.api.dependencies import (
    get_operator_identity_provider,
    get_outbox_recovery_service,
)
from sre_agent.api.main import create_app
from sre_agent.application.operator.read_models import (
    OperatorIdentity,
    OperatorUnauthenticated,
    UnavailableOperatorIdentityProvider,
)
from sre_agent.application.outbox.publish_events import (
    OutboxEventNotFound,
    OutboxPublishResult,
    PublishResultCode,
)
from sre_agent.application.outbox.recover_events import OutboxRecoveryBatchResult

EVENT_ID = UUID("60000000-0000-0000-0000-000000000001")
CORRELATION_ID = "outbox-recovery-request-1"
IDENTITY = OperatorIdentity("operator@example.com", global_access=True)


def test_outbox_recovery_routes_are_registered_before_dynamic_event_route() -> None:
    paths = list(create_app().openapi()["paths"])

    pending_path = "/api/v1/operations/outbox-events/retry-pending"
    failed_path = "/api/v1/operations/outbox-events/retry-failed"
    event_path = "/api/v1/operations/outbox-events/{eventId}/retry"

    assert pending_path in paths
    assert failed_path in paths
    assert event_path in paths
    assert paths.index(pending_path) < paths.index(event_path)
    assert paths.index(failed_path) < paths.index(event_path)


class TokenIdentityProvider:
    async def resolve(self, authorization: str | None) -> OperatorIdentity:
        if authorization != "Bearer operator-token":
            raise OperatorUnauthenticated
        return IDENTITY


class ScopedIdentityProvider:
    async def resolve(self, authorization: str | None) -> OperatorIdentity:
        assert authorization == "Bearer scoped-token"
        return OperatorIdentity("viewer@example.com")


class TruthyIdentityProvider:
    async def resolve(self, authorization: str | None) -> OperatorIdentity:
        assert authorization == "Bearer truthy-token"
        return OperatorIdentity(
            "truthy@example.com",
            global_access=1,  # pyright: ignore[reportArgumentType]
        )


class AcceptingIdentityProvider:
    async def resolve(self, authorization: str | None) -> OperatorIdentity:
        del authorization
        return IDENTITY


class FakeRecovery:
    def __init__(self) -> None:
        self.single_result = OutboxPublishResult(
            EVENT_ID,
            "PENDING",
            PublishResultCode.PUBLISHED,
        )
        self.batch_result = OutboxRecoveryBatchResult(
            selected=2,
            published=1,
            failed=1,
            no_op=0,
            failure_categories=("PUBLISH_ERROR",),
        )
        self.calls: list[tuple[str, object, OperatorIdentity, str]] = []
        self.missing = False

    async def retry_event(self, event_id, identity, correlation_id):
        self.calls.append(("event", event_id, identity, correlation_id))
        if self.missing:
            raise OutboxEventNotFound(str(event_id))
        return self.single_result

    async def retry_pending(self, limit, identity, correlation_id):
        self.calls.append(("pending", limit, identity, correlation_id))
        return self.batch_result

    async def retry_failed(self, limit, identity, correlation_id):
        self.calls.append(("failed", limit, identity, correlation_id))
        return self.batch_result


@asynccontextmanager
async def _client(
    provider: object,
    recovery: FakeRecovery,
    *,
    authorization: str | None = "Bearer operator-token",
):
    app = create_app()
    app.dependency_overrides[get_operator_identity_provider] = lambda: provider
    app.dependency_overrides[get_outbox_recovery_service] = lambda: recovery
    transport = httpx.ASGITransport(
        app=app,  # pyright: ignore[reportArgumentType]
        raise_app_exceptions=False,
    )
    headers = {"X-Correlation-ID": CORRELATION_ID}
    if authorization is not None:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://operations.test",
        headers=headers,
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_retry_event_returns_the_fixed_safe_camel_case_projection() -> None:
    recovery = FakeRecovery()
    async with _client(TokenIdentityProvider(), recovery) as client:
        response = await client.post(f"/api/v1/operations/outbox-events/{EVENT_ID}/retry")

    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == CORRELATION_ID
    assert response.json() == {
        "eventId": str(EVENT_ID),
        "previousStatus": "PENDING",
        "result": "PUBLISHED",
        "failureCategory": None,
    }
    assert recovery.calls == [("event", EVENT_ID, IDENTITY, CORRELATION_ID)]
    assert not {
        "payload",
        "topic",
        "project",
        "subscription",
        "attributes",
        "exception",
    } & set(response.json())


@pytest.mark.asyncio
async def test_published_event_recovery_is_a_no_op_success() -> None:
    recovery = FakeRecovery()
    recovery.single_result = OutboxPublishResult(
        EVENT_ID,
        "PUBLISHED",
        PublishResultCode.NO_OP,
    )
    async with _client(TokenIdentityProvider(), recovery) as client:
        response = await client.post(f"/api/v1/operations/outbox-events/{EVENT_ID}/retry")

    assert response.status_code == 200
    assert response.json()["result"] == "NO_OP"
    assert recovery.calls == [("event", EVENT_ID, IDENTITY, CORRELATION_ID)]


@pytest.mark.asyncio
async def test_retry_selectors_have_bounded_limits_and_fixed_aggregate_projection() -> (
    None
):
    recovery = FakeRecovery()
    async with _client(TokenIdentityProvider(), recovery) as client:
        pending = await client.post(
            "/api/v1/operations/outbox-events/retry-pending",
            params={"limit": 100},
        )
        failed = await client.post(
            "/api/v1/operations/outbox-events/retry-failed",
            params={"limit": 1},
        )
        invalid = await client.post(
            "/api/v1/operations/outbox-events/retry-pending",
            params={"limit": 101},
        )

    expected = {
        "selected": 2,
        "published": 1,
        "failed": 1,
        "noOp": 0,
        "failureCategories": ["PUBLISH_ERROR"],
    }
    assert pending.status_code == 200
    assert pending.json() == expected
    assert failed.status_code == 200
    assert failed.json() == expected
    assert invalid.status_code == 400
    assert recovery.calls == [
        ("pending", 100, IDENTITY, CORRELATION_ID),
        ("failed", 1, IDENTITY, CORRELATION_ID),
    ]


@pytest.mark.asyncio
async def test_recovery_rejects_request_bodies_without_invoking_the_service() -> None:
    recovery = FakeRecovery()
    async with _client(TokenIdentityProvider(), recovery) as client:
        response = await client.post(
            f"/api/v1/operations/outbox-events/{EVENT_ID}/retry",
            json={"payload": "forbidden"},
        )

    assert response.status_code == 400
    assert recovery.calls == []


@pytest.mark.asyncio
async def test_missing_event_maps_to_a_safe_404_problem() -> None:
    recovery = FakeRecovery()
    recovery.missing = True
    async with _client(TokenIdentityProvider(), recovery) as client:
        response = await client.post(f"/api/v1/operations/outbox-events/{EVENT_ID}/retry")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"
    assert str(EVENT_ID) not in response.json()["detail"]


@pytest.mark.asyncio
async def test_missing_bearer_is_unauthenticated_even_if_provider_would_resolve() -> (
    None
):
    recovery = FakeRecovery()
    async with _client(
        AcceptingIdentityProvider(), recovery, authorization=None
    ) as client:
        response = await client.post(
            f"/api/v1/operations/outbox-events/{EVENT_ID}/retry",
        )

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert recovery.calls == []


@pytest.mark.asyncio
async def test_rejected_bearer_is_unauthenticated() -> None:
    recovery = FakeRecovery()
    async with _client(TokenIdentityProvider(), recovery) as client:
        response = await client.post(
            f"/api/v1/operations/outbox-events/{EVENT_ID}/retry",
            headers={"Authorization": "Bearer rejected-token"},
        )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert recovery.calls == []


@pytest.mark.asyncio
async def test_non_global_identity_is_forbidden() -> None:
    recovery = FakeRecovery()
    async with _client(ScopedIdentityProvider(), recovery) as client:
        response = await client.post(
            f"/api/v1/operations/outbox-events/{EVENT_ID}/retry",
            headers={"Authorization": "Bearer scoped-token"},
        )

    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "SCOPE_FORBIDDEN"
    assert recovery.calls == []


@pytest.mark.asyncio
async def test_only_a_literal_true_global_access_grants_recovery() -> None:
    recovery = FakeRecovery()
    async with _client(TruthyIdentityProvider(), recovery) as client:
        response = await client.post(
            f"/api/v1/operations/outbox-events/{EVENT_ID}/retry",
            headers={"Authorization": "Bearer truthy-token"},
        )

    assert response.status_code == 403
    assert recovery.calls == []


@pytest.mark.asyncio
async def test_unconfigured_operator_identity_provider_stays_fail_closed() -> None:
    recovery = FakeRecovery()
    async with _client(UnavailableOperatorIdentityProvider(), recovery) as client:
        response = await client.post(f"/api/v1/operations/outbox-events/{EVENT_ID}/retry")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert recovery.calls == []


def test_outbox_recovery_openapi_has_stable_schemas_and_error_responses() -> None:
    openapi = create_app().openapi()
    event_operation = openapi["paths"][
        "/api/v1/operations/outbox-events/{eventId}/retry"
    ]["post"]
    batch_operation = openapi["paths"][
        "/api/v1/operations/outbox-events/retry-pending"
    ]["post"]

    assert {"401", "403", "404"} <= set(event_operation["responses"])
    assert {"401", "403", "404"} <= set(batch_operation["responses"])
    schemas = openapi["components"]["schemas"]
    assert set(schemas["OutboxRetryEventResponse"]["properties"]) == {
        "eventId",
        "previousStatus",
        "result",
        "failureCategory",
    }
    assert set(schemas["OutboxRetryEventResponse"]["required"]) == {
        "eventId",
        "previousStatus",
        "result",
        "failureCategory",
    }
    assert set(schemas["OutboxRetryBatchResponse"]["properties"]) == {
        "selected",
        "published",
        "failed",
        "noOp",
        "failureCategories",
    }
    assert set(schemas["OutboxRetryBatchResponse"]["required"]) == {
        "selected",
        "published",
        "failed",
        "noOp",
        "failureCategories",
    }
