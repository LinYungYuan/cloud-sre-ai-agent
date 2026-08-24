from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response

from sre_agent.api.dependencies import (
    get_operator_identity_provider,
    get_operator_read_service,
)
from sre_agent.api.schemas.operator import (
    AlertDetail,
    CursorPageIncidents,
    CursorPageRcaRuns,
    IncidentDetail,
    RcaReport,
    TraceWaterfallResponse,
)
from sre_agent.application.operator.read_models import (
    OperatorIdentity,
    OperatorIdentityProvider,
    OperatorReadService,
)

router = APIRouter(prefix="/api/v1", tags=["operator"])


async def _identity(
    provider: Annotated[
        OperatorIdentityProvider, Depends(get_operator_identity_provider)
    ],
    authorization: Annotated[str | None, Header()] = None,
) -> OperatorIdentity:
    return await provider.resolve(authorization)


@router.get("/incidents", response_model=CursorPageIncidents)
async def list_incidents(
    service: Annotated[OperatorReadService, Depends(get_operator_read_service)],
    identity: Annotated[OperatorIdentity, Depends(_identity)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    return await service.list_incidents(
        identity,
        cursor=cursor,
        limit=limit,
    )


@router.get("/incidents/{id}", response_model=IncidentDetail)
async def get_incident(
    id: UUID,
    response: Response,
    service: Annotated[OperatorReadService, Depends(get_operator_read_service)],
    identity: Annotated[OperatorIdentity, Depends(_identity)],
) -> dict[str, object]:
    item = await service.get_incident(identity, id)
    response.headers["ETag"] = f'W/"{item["version"]}"'
    return item


@router.get("/alerts/{id}", response_model=AlertDetail)
async def get_alert(
    id: UUID,
    service: Annotated[OperatorReadService, Depends(get_operator_read_service)],
    identity: Annotated[OperatorIdentity, Depends(_identity)],
) -> dict[str, object]:
    return await service.get_alert(identity, id)


@router.get("/incidents/{id}/rca-runs", response_model=CursorPageRcaRuns)
async def list_rca_runs(
    id: UUID,
    service: Annotated[OperatorReadService, Depends(get_operator_read_service)],
    identity: Annotated[OperatorIdentity, Depends(_identity)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    return await service.list_rca_runs(
        identity,
        id,
        cursor=cursor,
        limit=limit,
    )


@router.get("/rca-runs/{id}/report", response_model=RcaReport)
async def get_rca_report(
    id: UUID,
    service: Annotated[OperatorReadService, Depends(get_operator_read_service)],
    identity: Annotated[OperatorIdentity, Depends(_identity)],
) -> dict[str, object]:
    return await service.get_rca_report(identity, id)


@router.get(
    "/rca-runs/{id}/trace-waterfall",
    response_model=TraceWaterfallResponse,
)
async def get_trace_waterfall(
    id: UUID,
    service: Annotated[OperatorReadService, Depends(get_operator_read_service)],
    identity: Annotated[OperatorIdentity, Depends(_identity)],
) -> dict[str, object]:
    return await service.get_trace_waterfall(identity, id)
