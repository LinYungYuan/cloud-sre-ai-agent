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
from sre_agent.domain.alerts.normalization import (
    AlertIssue,
    CanonicalBaseAlert,
    NormalizationStatus,
    NormalizedResource,
    SafeRuleEngine,
)
from sre_agent.domain.alerts.provider import Provider, detect_provider
from sre_agent.domain.alerts.severity import SeverityDecision, map_severity
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
    labels: Mapping[str, object]
    annotations: Mapping[str, str]
    values: Mapping[str, FrozenJsonValue]
    generator_url: str
    silence_url: str | None
    dashboard_url: str | None
    panel_url: str | None
    image_url: str | None
    provider: Provider
    folder_code: str | None
    alert_name: str | None
    severity: SeverityDecision
    issue: AlertIssue
    resource: NormalizedResource | None
    normalization_status: NormalizationStatus
    normalization_rule_id: UUID | None
    normalization_rule_version: int | None
    normalization_warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        frozen_labels = _deep_freeze_json(self.labels)
        if not isinstance(frozen_labels, Mapping):
            raise TypeError("alert labels must be a JSON object")
        object.__setattr__(self, "labels", frozen_labels)
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
    rule_engine: SafeRuleEngine,
) -> CanonicalAlertEvent:
    status = AlertState(alert.status.upper())
    provider = detect_provider(alert.labels)
    severity = map_severity(alert.labels.get("severity"))
    base_alert = CanonicalBaseAlert(
        labels=alert.labels,
        annotations=alert.annotations,
        values=alert.values,
    )
    normalization = rule_engine.normalize(base_alert, provider)
    folder = alert.labels.get("folder")
    alert_name = alert.labels.get("alertname")
    issue_text = alert.annotations.get("AlertValues")
    issue_warnings = () if isinstance(issue_text, str) else ("alert_values_missing",)
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
        provider=provider.provider,
        folder_code=folder.strip() if isinstance(folder, str) and folder.strip() else None,
        alert_name=(
            alert_name.strip()
            if isinstance(alert_name, str) and alert_name.strip()
            else None
        ),
        severity=severity,
        issue=AlertIssue(raw_text=issue_text if isinstance(issue_text, str) else ""),
        resource=normalization.resource,
        normalization_status=normalization.status,
        normalization_rule_id=normalization.rule_id,
        normalization_rule_version=normalization.rule_version,
        normalization_warnings=(
            severity.warnings + issue_warnings + normalization.warnings
        ),
    )


def normalize_alerts(
    source_id: UUID,
    webhook: GrafanaWebhook,
    rule_engine: SafeRuleEngine | None = None,
) -> tuple[CanonicalAlertEvent, ...]:
    raw_sha256 = hash_raw_body(webhook.raw_body)
    engine = rule_engine or SafeRuleEngine(())
    return tuple(
        _normalize_alert(source_id, alert, raw_sha256, engine)
        for alert in webhook.alerts
    )
