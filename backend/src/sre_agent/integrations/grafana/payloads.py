from typing import Any, Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from sre_agent.domain.common import UtcTimestamp


class GrafanaPayloadTooLarge(Exception):
    """Raised when the raw Grafana request body exceeds its configured limit."""


class GrafanaPayloadInvalid(Exception):
    """Raised when a Grafana request body cannot be parsed or validated."""


class GrafanaAlert(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: UtcTimestamp = Field(
        validation_alias="startsAt", serialization_alias="startsAt"
    )
    ends_at: UtcTimestamp = Field(
        validation_alias="endsAt", serialization_alias="endsAt"
    )
    values: dict[str, Any]
    generator_url: AnyUrl = Field(
        validation_alias="generatorURL", serialization_alias="generatorURL"
    )
    fingerprint: str
    silence_url: AnyUrl | None = Field(
        default=None, validation_alias="silenceURL", serialization_alias="silenceURL"
    )
    dashboard_url: AnyUrl | Literal[""] | None = Field(
        default=None, validation_alias="dashboardURL", serialization_alias="dashboardURL"
    )
    panel_url: AnyUrl | Literal[""] | None = Field(
        default=None, validation_alias="panelURL", serialization_alias="panelURL"
    )
    image_url: AnyUrl | None = Field(
        default=None, validation_alias="imageURL", serialization_alias="imageURL"
    )


class GrafanaWebhook(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    receiver: str | None = None
    status: Literal["firing", "resolved"]
    org_id: int | None = Field(
        default=None, validation_alias="orgId", serialization_alias="orgId"
    )
    alerts: list[GrafanaAlert]
    group_labels: dict[str, str] | None = Field(
        default=None, validation_alias="groupLabels", serialization_alias="groupLabels"
    )
    common_labels: dict[str, str] | None = Field(
        default=None, validation_alias="commonLabels", serialization_alias="commonLabels"
    )
    common_annotations: dict[str, str] | None = Field(
        default=None,
        validation_alias="commonAnnotations",
        serialization_alias="commonAnnotations",
    )
    external_url: AnyUrl | None = Field(
        default=None, validation_alias="externalURL", serialization_alias="externalURL"
    )
    version: str | None = None
    group_key: str | None = Field(
        default=None, validation_alias="groupKey", serialization_alias="groupKey"
    )
    truncated_alerts: int | None = Field(
        default=None,
        ge=0,
        validation_alias="truncatedAlerts",
        serialization_alias="truncatedAlerts",
    )
    title: str | None = None
    state: str | None = None
    message: str | None = None
    _raw_body: bytes = PrivateAttr()

    @property
    def raw_body(self) -> bytes:
        return self._raw_body


def parse_grafana_body(raw_body: bytes, max_bytes: int) -> GrafanaWebhook:
    if len(raw_body) > max_bytes:
        raise GrafanaPayloadTooLarge("Grafana webhook body exceeds the configured limit")

    try:
        webhook = GrafanaWebhook.model_validate_json(raw_body)
    except ValidationError:
        raise GrafanaPayloadInvalid("invalid Grafana webhook payload") from None

    webhook._raw_body = raw_body
    return webhook
