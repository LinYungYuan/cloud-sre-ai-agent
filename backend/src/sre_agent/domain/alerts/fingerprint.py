import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from sre_agent.domain.alerts.models import AlertState
from sre_agent.domain.common import require_aware_utc


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def make_alert_fingerprint(
    source_id: UUID,
    grafana_fingerprint: str,
    labels: Mapping[str, object],
) -> str:
    if grafana_fingerprint:
        return grafana_fingerprint

    return _canonical_sha256(
        {
            "labels": dict(sorted(labels.items())),
            "source_id": str(source_id),
        }
    )


def hash_raw_body(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def make_dedup_key(
    source_id: UUID,
    fingerprint: str,
    status: AlertState,
    starts_at: datetime,
    ends_at: datetime,
    raw_sha256: str,
) -> str:
    canonical_starts_at = require_aware_utc(starts_at)
    canonical_ends_at = require_aware_utc(ends_at)
    return _canonical_sha256(
        {
            "source_id": str(source_id),
            "fingerprint": fingerprint,
            "status": status.value,
            "starts_at": canonical_starts_at.isoformat(),
            "ends_at": canonical_ends_at.isoformat(),
            "raw_sha256": raw_sha256,
        }
    )
