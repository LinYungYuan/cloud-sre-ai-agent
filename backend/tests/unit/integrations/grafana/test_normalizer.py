import hashlib
import json
from uuid import UUID

from sre_agent.domain.alerts.models import AlertState
from sre_agent.integrations.grafana.normalizer import normalize_alerts
from sre_agent.integrations.grafana.payloads import parse_grafana_body

SOURCE = UUID("00000000-0000-0000-0000-000000000001")


def _alert(*, fingerprint: str, status: str, service: str) -> dict[str, object]:
    return {
        "status": status,
        "labels": {"service": service, "alertname": "HighCPU"},
        "annotations": {"summary": f"{service} CPU is high"},
        "startsAt": "2026-08-12T02:00:00Z",
        "endsAt": "2026-08-12T03:00:00Z",
        "values": {"A": 95},
        "generatorURL": f"https://grafana.example.com/alert/{service}",
        "fingerprint": fingerprint,
        "dashboardURL": "",
        "panelURL": "",
    }


def _parse(body: dict[str, object], *, pretty: bool = False):
    raw = json.dumps(body, indent=2 if pretty else None).encode()
    return raw, parse_grafana_body(raw, max_bytes=1_048_576)


def test_grouped_webhook_expands_one_to_one_and_hashes_exact_raw_bytes():
    body = {
        "status": "firing",
        "alerts": [
            _alert(fingerprint="grafana-api", status="firing", service="api"),
            _alert(fingerprint="", status="resolved", service="worker"),
        ],
    }
    raw_body, webhook = _parse(body, pretty=True)

    events = normalize_alerts(SOURCE, webhook)

    assert len(events) == len(webhook.alerts) == 2
    assert [event.status for event in events] == [
        AlertState.FIRING,
        AlertState.RESOLVED,
    ]
    assert events[0].fingerprint == "grafana-api"
    assert events[1].fingerprint == (
        "080bc222f4f7bcd7deabf717b3ec2b58a499e566ba2ce0bdf5d3991e60f867ab"
    )
    assert {event.raw_sha256 for event in events} == {
        hashlib.sha256(raw_body).hexdigest()
    }
    assert events[0].labels == {"alertname": "HighCPU", "service": "api"}
    assert events[1].annotations == {"summary": "worker CPU is high"}


def test_semantically_equal_bodies_have_distinct_delivery_hashes_and_dedup_keys():
    body = {
        "status": "firing",
        "alerts": [_alert(fingerprint="grafana-api", status="firing", service="api")],
    }
    compact_raw, compact_webhook = _parse(body)
    pretty_raw, pretty_webhook = _parse(body, pretty=True)

    compact = normalize_alerts(SOURCE, compact_webhook)[0]
    pretty = normalize_alerts(SOURCE, pretty_webhook)[0]

    assert compact_raw != pretty_raw
    assert compact.raw_sha256 == hashlib.sha256(compact_raw).hexdigest()
    assert pretty.raw_sha256 == hashlib.sha256(pretty_raw).hexdigest()
    assert compact.raw_sha256 != pretty.raw_sha256
    assert compact.dedup_key != pretty.dedup_key
