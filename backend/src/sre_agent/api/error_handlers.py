from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from sre_agent.integrations.grafana.authenticator import GrafanaUnauthorized
from sre_agent.integrations.grafana.payloads import (
    GrafanaPayloadInvalid,
    GrafanaPayloadTooLarge,
)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GrafanaUnauthorized)
    async def handle_unauthorized(
        request: Request, error: GrafanaUnauthorized
    ) -> JSONResponse:
        del error
        return _problem(
            request,
            status=401,
            problem_type="urn:sre-agent:problem:grafana-unauthorized",
            title="Unauthorized",
            detail="A valid Grafana bearer token is required.",
        )

    @app.exception_handler(GrafanaPayloadInvalid)
    async def handle_invalid_payload(
        request: Request, error: GrafanaPayloadInvalid
    ) -> JSONResponse:
        del error
        return _invalid_request(request)

    @app.exception_handler(GrafanaPayloadTooLarge)
    async def handle_large_payload(
        request: Request, error: GrafanaPayloadTooLarge
    ) -> JSONResponse:
        del error
        return _problem(
            request,
            status=413,
            problem_type="urn:sre-agent:problem:grafana-payload-too-large",
            title="Payload too large",
            detail="The Grafana webhook body exceeds the allowed size.",
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        del error
        return _invalid_request(request)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, error: Exception) -> JSONResponse:
        del error
        return _problem(
            request,
            status=500,
            problem_type="urn:sre-agent:problem:internal-server-error",
            title="Internal server error",
            detail="An unexpected error occurred.",
        )


def _invalid_request(request: Request) -> JSONResponse:
    return _problem(
        request,
        status=400,
        problem_type="urn:sre-agent:problem:invalid-grafana-request",
        title="Invalid request",
        detail="The Grafana webhook request is invalid.",
    )


def _problem(
    request: Request,
    *,
    status: int,
    problem_type: str,
    title: str,
    detail: str,
) -> JSONResponse:
    correlation_id = request.state.correlation_id
    content: dict[str, Any] = {
        "type": problem_type,
        "title": title,
        "status": status,
        "detail": detail,
        "correlationId": correlation_id,
    }
    return JSONResponse(
        status_code=status,
        content=content,
        media_type="application/problem+json",
        headers={"X-Correlation-ID": correlation_id},
    )
