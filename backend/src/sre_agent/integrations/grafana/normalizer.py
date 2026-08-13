from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TypeAlias
from uuid import UUID

from sre_agent.domain.alerts.fingerprint import (
    hash_raw_body,
    make_alert_fingerprint,
    make_dedup_key,
)
from sre_agent.domain.alerts.models import AlertState
from sre_agent.integrations.grafana.payloads import GrafanaAlert, GrafanaWebhook

JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJsonValue: TypeAlias = (
    JsonScalar | tuple["FrozenJsonValue", ...] | Mapping[str, "FrozenJsonValue"]
)


def _deep_freeze_json(value: object) -> FrozenJsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType(
            {
                key: _deep_freeze_json(nested)
                for key, nested in sorted(value.items())
            }
        )
    if isinstance(value, list | tuple):
        return tuple(_deep_freeze_json(nested) for nested in value)
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class CanonicalAlertEvent:
    source_id: UUID
    fingerprint: str
    dedup_key: str
    raw_sha256: str
    status: AlertState
    starts_at: datetime
    ends_at: datetime
    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    values: Mapping[str, FrozenJsonValue]
    generator_url: str
    silence_url: str | None
    dashboard_url: str | None
    panel_url: str | None
    image_url: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "labels",
            MappingProxyType(dict(sorted(self.labels.items()))),
        )
        object.__setattr__(
            self,
            "annotations",
            MappingProxyType(dict(sorted(self.annotations.items()))),
        )
        frozen_values = _deep_freeze_json(self.values)
        if not isinstance(frozen_values, Mapping):
            raise TypeError("alert values must be a JSON object")
        object.__setattr__(self, "values", frozen_values)


def _optional_url(value: object | None) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _normalize_alert(
    source_id: UUID,
    alert: GrafanaAlert,
    raw_sha256: str,
) -> CanonicalAlertEvent:
    status = AlertState(alert.status.upper())
    fingerprint = make_alert_fingerprint(
        source_id=source_id,
        grafana_fingerprint=alert.fingerprint,
        labels=alert.labels,
    )
    return CanonicalAlertEvent(
        source_id=source_id,
        fingerprint=fingerprint,
        dedup_key=make_dedup_key(
            source_id=source_id,
            fingerprint=fingerprint,
            status=status,
            starts_at=alert.starts_at,
            ends_at=alert.ends_at,
            raw_sha256=raw_sha256,
        ),
        raw_sha256=raw_sha256,
        status=status,
        starts_at=alert.starts_at,
        ends_at=alert.ends_at,
        labels=alert.labels,
        annotations=alert.annotations,
        values=alert.values,
        generator_url=str(alert.generator_url),
        silence_url=_optional_url(alert.silence_url),
        dashboard_url=_optional_url(alert.dashboard_url),
        panel_url=_optional_url(alert.panel_url),
        image_url=_optional_url(alert.image_url),
    )


def normalize_alerts(
    source_id: UUID,
    webhook: GrafanaWebhook,
) -> tuple[CanonicalAlertEvent, ...]:
    raw_sha256 = hash_raw_body(webhook.raw_body)
    return tuple(
        _normalize_alert(source_id, alert, raw_sha256) for alert in webhook.alerts
    )
