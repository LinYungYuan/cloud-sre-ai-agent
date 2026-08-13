import hashlib
from dataclasses import dataclass
from uuid import UUID

from sre_agent.domain.alerts.cross_cloud import AlertValidationError


@dataclass(frozen=True, slots=True)
class IncidentIdentity:
    key: str
    version: int
    parts: tuple[str, ...]
    errors: tuple[AlertValidationError, ...]


def _encode(parts: tuple[str, ...]) -> bytes:
    encoded_parts = (part.encode("utf-8") for part in parts)
    return b"".join(len(part).to_bytes(4, "big") + part for part in encoded_parts)


def make_incident_identity_v2(
    source_id: UUID,
    folder: str | None,
    alert_name: str | None,
    fingerprint: str,
) -> IncidentIdentity:
    errors: list[AlertValidationError] = []
    normalized_folder = folder.strip() if isinstance(folder, str) else ""
    normalized_alert_name = alert_name.strip() if isinstance(alert_name, str) else ""
    normalized_fingerprint = fingerprint.strip()
    if not normalized_folder:
        errors.append(AlertValidationError(field="folder", code="required"))
    if not normalized_alert_name:
        errors.append(AlertValidationError(field="alertname", code="required"))
    if not normalized_fingerprint:
        errors.append(AlertValidationError(field="fingerprint", code="required"))

    if errors:
        parts = (
            "2",
            str(source_id),
            "__invalid__",
            normalized_fingerprint or "__missing__",
        )
    else:
        parts = ("2", str(source_id), normalized_folder, normalized_alert_name)

    return IncidentIdentity(
        key=hashlib.sha256(_encode(parts)).hexdigest(),
        version=2,
        parts=parts,
        errors=tuple(errors),
    )
