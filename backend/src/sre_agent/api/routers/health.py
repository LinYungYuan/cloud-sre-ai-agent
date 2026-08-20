import asyncio

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse

from sre_agent.api.composition import ReadinessCheck
from sre_agent.api.dependencies import get_readiness_check

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    check: ReadinessCheck = Depends(get_readiness_check),  # noqa: B008 - FastAPI injects the composition-owned check.
) -> Response:
    try:
        await asyncio.wait_for(check(), timeout=2.0)
    except TimeoutError:
        return _unavailable_response()
    except Exception:  # noqa: BLE001 - readiness errors must never leak details.
        return _unavailable_response()
    return Response(status_code=status.HTTP_200_OK)


def _unavailable_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "service unavailable"},
    )
