from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_serializer

from sre_agent.api.dependencies import (
    get_grafana_authenticator,
    get_ingest_grafana_alerts,
)
from sre_agent.application.alerts.ingest_grafana_alerts import IngestGrafanaAlerts
from sre_agent.integrations.grafana.authenticator import GrafanaTokenAuthenticator
from sre_agent.integrations.grafana.payloads import (
    GrafanaPayloadInvalid,
    GrafanaPayloadTooLarge,
)

router = APIRouter(prefix="/webhooks/v1/grafana", tags=["grafana-webhook"])
MAX_BODY_BYTES = 1_048_576


class WebhookAccepted(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    delivery_id: UUID = Field(serialization_alias="deliveryId")
    accepted_at: datetime = Field(serialization_alias="acceptedAt")

    @field_serializer("accepted_at")
    def serialize_accepted_at(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@router.post(
    "/{sourceId}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAccepted,
)
async def ingest_grafana_webhook(
    request: Request,
    source_id: Annotated[UUID, Path(alias="sourceId")],
    authenticator: Annotated[
        GrafanaTokenAuthenticator, Depends(get_grafana_authenticator)
    ],
    ingestion: Annotated[IngestGrafanaAlerts, Depends(get_ingest_grafana_alerts)],
) -> WebhookAccepted:
    if _media_type(request.headers.get("content-type")) != "application/json":
        raise GrafanaPayloadInvalid("Grafana webhook Content-Type must be JSON")

    token_id = authenticator.verify(
        source_id,
        request.headers.get("authorization"),
    )
    raw_body = await _read_bounded_body(request, MAX_BODY_BYTES)
    result = await ingestion.execute(
        source_id=source_id,
        token_id=token_id,
        raw_body=raw_body,
        received_at=datetime.now(UTC),
    )
    return WebhookAccepted(
        delivery_id=result.delivery_id,
        accepted_at=result.accepted_at,
    )


def _media_type(content_type: str | None) -> str:
    if content_type is None:
        return ""
    return content_type.partition(";")[0].strip().lower()


async def _read_bounded_body(request: Request, max_bytes: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        remaining = max_bytes + 1 - len(body)
        if remaining <= 0:
            break
        body.extend(chunk[:remaining])
        if len(body) > max_bytes:
            raise GrafanaPayloadTooLarge(
                "Grafana webhook body exceeds the configured limit"
            )
    return bytes(body)
