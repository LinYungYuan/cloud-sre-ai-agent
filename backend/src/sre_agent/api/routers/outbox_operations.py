from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request

from sre_agent.api.dependencies import (
    get_operator_identity_provider,
    get_outbox_recovery_service,
)
from sre_agent.api.schemas.outbox_operations import (
    OutboxRetryBatchResponse,
    OutboxRetryEventResponse,
)
from sre_agent.application.operator.read_models import (
    OperatorForbidden,
    OperatorIdentity,
    OperatorIdentityProvider,
    OperatorUnauthenticated,
)
from sre_agent.application.outbox.recover_events import (
    OutboxRecoveryRequestBodyForbidden,
    OutboxRecoveryService,
)

router = APIRouter(prefix="/api/v1/operations", tags=["operator"])

_RECOVERY_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Invalid recovery request."},
    401: {"description": "Operator authentication is missing or invalid."},
    403: {"description": "Global operator access is required."},
    404: {"description": "The outbox event does not exist."},
    500: {"description": "An internal or dependency failure occurred."},
}


async def _identity(
    provider: Annotated[
        OperatorIdentityProvider, Depends(get_operator_identity_provider)
    ],
    authorization: Annotated[str | None, Header()] = None,
) -> OperatorIdentity:
    identity = await provider.resolve(authorization)
    if authorization is None or not authorization.startswith("Bearer "):
        raise OperatorUnauthenticated
    if identity.global_access is not True:
        raise OperatorForbidden
    return identity


async def _no_request_body(request: Request) -> None:
    if await request.body():
        raise OutboxRecoveryRequestBodyForbidden


@router.post(
    "/outbox-events/retry-pending",
    response_model=OutboxRetryBatchResponse,
    responses=_RECOVERY_ERROR_RESPONSES,
)
async def retry_pending(
    request: Request,
    service: Annotated[OutboxRecoveryService, Depends(get_outbox_recovery_service)],
    identity: Annotated[OperatorIdentity, Depends(_identity)],
    _: Annotated[None, Depends(_no_request_body)],
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> object:
    del _
    return await service.retry_pending(
        limit,
        identity,
        request.state.correlation_id,
    )


@router.post(
    "/outbox-events/retry-failed",
    response_model=OutboxRetryBatchResponse,
    responses=_RECOVERY_ERROR_RESPONSES,
)
async def retry_failed(
    request: Request,
    service: Annotated[OutboxRecoveryService, Depends(get_outbox_recovery_service)],
    identity: Annotated[OperatorIdentity, Depends(_identity)],
    _: Annotated[None, Depends(_no_request_body)],
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> object:
    del _
    return await service.retry_failed(
        limit,
        identity,
        request.state.correlation_id,
    )


@router.post(
    "/outbox-events/{eventId}/retry",
    response_model=OutboxRetryEventResponse,
    responses=_RECOVERY_ERROR_RESPONSES,
)
async def retry_event(
    request: Request,
    event_id: Annotated[UUID, Path(alias="eventId")],
    service: Annotated[OutboxRecoveryService, Depends(get_outbox_recovery_service)],
    identity: Annotated[OperatorIdentity, Depends(_identity)],
    _: Annotated[None, Depends(_no_request_body)],
) -> object:
    del _
    return await service.retry_event(
        event_id,
        identity,
        request.state.correlation_id,
    )
