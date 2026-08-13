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
_AWS_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_RESOURCE_TYPE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_GCP_RESOURCE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~+%-]*$")
_AWS_ARN_FIELD = re.compile(r"^[a-z0-9-]+$")


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
            if self._is_invalid_value(
                field,
                value,
                labels.get("cloud_provider"),
                labels.get("cloud_scope_id"),
            ):
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
        cloud_scope_id: str | None,
    ) -> bool:
        return (
            (field == "cloud_provider" and value not in ALLOWED_CLOUD_PROVIDERS)
            or (
                field == "cloud_scope_id"
                and not CrossCloudAlertValidator._is_valid_scope_id(
                    cloud_provider, value
                )
            )
            or (
                field == "resource_type"
                and (len(value) > 64 or _RESOURCE_TYPE.fullmatch(value) is None)
            )
            or (
                field == "resource_id"
                and not CrossCloudAlertValidator._is_valid_resource_id(
                    cloud_provider,
                    cloud_scope_id,
                    value,
                )
            )
            or (field == "severity" and value not in ALLOWED_SEVERITIES)
            or (field == "signal_type" and value not in ALLOWED_SIGNAL_TYPES)
        )

    @staticmethod
    def _is_valid_scope_id(cloud_provider: str | None, scope_id: str) -> bool:
        if cloud_provider == "aws":
            return _AWS_ACCOUNT_ID.fullmatch(scope_id) is not None
        if cloud_provider == "gcp":
            return _GCP_PROJECT_ID.fullmatch(scope_id) is not None
        return True

    @staticmethod
    def _is_valid_resource_id(
        cloud_provider: str | None,
        cloud_scope_id: str | None,
        resource_id: str,
    ) -> bool:
        if cloud_provider == "gcp":
            if (
                cloud_scope_id is None
                or _GCP_PROJECT_ID.fullmatch(cloud_scope_id) is None
            ):
                return True
            segments = resource_id.split("/")
            return (
                cloud_scope_id is not None
                and len(segments) >= 4
                and len(segments) % 2 == 0
                and segments[:2] == ["projects", cloud_scope_id]
                and all(
                    _GCP_RESOURCE_SEGMENT.fullmatch(segment) is not None
                    for segment in segments[2:]
                )
            )
        if cloud_provider == "aws":
            if (
                cloud_scope_id is None
                or _AWS_ACCOUNT_ID.fullmatch(cloud_scope_id) is None
            ):
                return True
            arn = resource_id.split(":", 5)
            return (
                cloud_scope_id is not None
                and len(arn) == 6
                and arn[0] == "arn"
                and arn[1].startswith("aws")
                and _AWS_ARN_FIELD.fullmatch(arn[1]) is not None
                and _AWS_ARN_FIELD.fullmatch(arn[2]) is not None
                and (not arn[3] or _AWS_ARN_FIELD.fullmatch(arn[3]) is not None)
                and arn[4] == cloud_scope_id
                and bool(arn[5])
                and not any(character.isspace() for character in arn[5])
            )
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
