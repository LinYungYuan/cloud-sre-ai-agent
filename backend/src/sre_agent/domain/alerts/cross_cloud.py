import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

REQUIRED_LABELS = (
    "alertname",
    "cloud_provider",
    "cloud_scope_id",
    "resource_type",
    "resource_id",
    "environment",
    "service",
    "team",
    "severity",
    "signal_type",
)
ALLOWED_CLOUD_PROVIDERS = frozenset({"gcp", "aws"})
ALLOWED_SEVERITIES = frozenset({"critical", "warning", "info"})
ALLOWED_SIGNAL_TYPES = frozenset({"metric", "log", "trace", "synthetic"})
INCIDENT_IDENTITY_FIELDS = (
    "cloud_provider",
    "cloud_scope_id",
    "resource_type",
    "resource_id",
    "alertname",
)

_GCP_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_AWS_ACCOUNT_ID = re.compile(r"^\d{12}$")


@dataclass(frozen=True, slots=True)
class AlertValidationError:
    field: str
    code: str


@dataclass(frozen=True, slots=True)
class CrossCloudValidationResult:
    is_valid: bool
    errors: tuple[AlertValidationError, ...]


class CrossCloudAlertValidator:
    def validate(self, labels: Mapping[str, str]) -> CrossCloudValidationResult:
        errors: list[AlertValidationError] = []
        for field in REQUIRED_LABELS:
            value = labels.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(AlertValidationError(field=field, code="required"))
                continue
            if self._is_invalid_value(field, value, labels.get("cloud_provider")):
                errors.append(AlertValidationError(field=field, code="invalid_value"))

        return CrossCloudValidationResult(
            is_valid=not errors,
            errors=tuple(errors),
        )

    @staticmethod
    def _is_invalid_value(
        field: str,
        value: str,
        cloud_provider: str | None,
    ) -> bool:
        return (
            field == "cloud_provider" and value not in ALLOWED_CLOUD_PROVIDERS
        ) or (
            field == "cloud_scope_id"
            and not CrossCloudAlertValidator._is_valid_scope_id(cloud_provider, value)
        ) or (field == "severity" and value not in ALLOWED_SEVERITIES) or (
            field == "signal_type" and value not in ALLOWED_SIGNAL_TYPES
        )

    @staticmethod
    def _is_valid_scope_id(cloud_provider: str | None, scope_id: str) -> bool:
        if cloud_provider == "aws":
            return _AWS_ACCOUNT_ID.fullmatch(scope_id) is not None
        if cloud_provider == "gcp":
            return _GCP_PROJECT_ID.fullmatch(scope_id) is not None
        return True


def make_incident_identity(source_id: UUID, labels: Mapping[str, str]) -> str:
    validation = CrossCloudAlertValidator().validate(labels)
    if not validation.is_valid:
        raise ValueError("invalid cross-cloud alert labels")

    canonical = {
        "source_id": str(source_id),
        **{field: labels[field] for field in INCIDENT_IDENTITY_FIELDS},
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
