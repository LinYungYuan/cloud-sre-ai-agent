from datetime import UTC, datetime
from uuid import UUID

from sre_agent.domain.alerts.fingerprint import (
    make_alert_fingerprint,
    make_dedup_key,
)
from sre_agent.domain.alerts.models import AlertState

SOURCE_ONE = UUID("00000000-0000-0000-0000-000000000001")
SOURCE_TWO = UUID("00000000-0000-0000-0000-000000000002")


def test_non_empty_grafana_fingerprint_is_the_alert_identity():
    fingerprint = make_alert_fingerprint(
        source_id=SOURCE_ONE,
        grafana_fingerprint="grafana-fp",
        labels={"alertname": "HighCPU"},
    )

    assert fingerprint == "grafana-fp"


def test_fallback_fingerprint_uses_sorted_labels_and_source_id():
    first = make_alert_fingerprint(
        source_id=SOURCE_ONE,
        grafana_fingerprint="",
        labels={"service": "api", "alertname": "HighCPU"},
    )
    reordered = make_alert_fingerprint(
        source_id=SOURCE_ONE,
        grafana_fingerprint="",
        labels={"alertname": "HighCPU", "service": "api"},
    )
    other_source = make_alert_fingerprint(
        source_id=SOURCE_TWO,
        grafana_fingerprint="",
        labels={"alertname": "HighCPU", "service": "api"},
    )

    assert first == "546ced17ec47f5a720e1b3c06db448c560a8c870bf56c824c7bcf1833abaf086"
    assert reordered == first
    assert other_source == (
        "a84314cf3e2b95afbe8f47dc3edd93b01167020a2081830114b62185f0e661c4"
    )


def test_dedup_key_includes_the_alert_lifecycle_identity():
    starts_at = datetime(2026, 8, 12, 2, tzinfo=UTC)
    ends_at = datetime(2026, 8, 12, 3, tzinfo=UTC)

    firing = make_dedup_key(
        source_id=SOURCE_ONE,
        fingerprint="grafana-fp",
        status=AlertState.FIRING,
        starts_at=starts_at,
        ends_at=ends_at,
        raw_sha256="a" * 64,
    )
    resolved = make_dedup_key(
        source_id=SOURCE_ONE,
        fingerprint="grafana-fp",
        status=AlertState.RESOLVED,
        starts_at=starts_at,
        ends_at=ends_at,
        raw_sha256="a" * 64,
    )

    assert firing == "798867dee00e08f232d34fadde317767e3dc7cd7f90d43af3ed8d3680f1ada76"
    assert resolved == "b6d2398b590ef60d585680c26b563e5d98141c97e5075b45a3491e9a685ed75c"
    assert resolved != firing
