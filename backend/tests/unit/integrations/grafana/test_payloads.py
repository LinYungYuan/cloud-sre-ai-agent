import json
from datetime import UTC
from pathlib import Path

import pytest

from sre_agent.integrations.grafana.payloads import (
    GrafanaPayloadInvalid,
    GrafanaPayloadTooLarge,
    GrafanaWebhook,
    parse_grafana_body,
)

EXAMPLE_PATH = (
    Path(__file__).resolve().parents[5]
    / "contracts"
    / "examples"
    / "grafana-firing.json"
)


def test_parse_grafana_body_preserves_the_exact_original_bytes_and_extensions():
    raw_body = EXAMPLE_PATH.read_bytes()

    webhook = parse_grafana_body(raw_body, max_bytes=1_048_576)

    assert webhook.raw_body is raw_body
    assert webhook.model_extra == {"grafanaTopLevelExtension": "retain-me"}
    assert webhook.alerts[0].model_extra == {
        "grafanaExtension": {"runbookOwner": "payments"}
    }


def test_parse_grafana_body_normalizes_offset_timestamps_to_aware_utc():
    body = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {},
                "annotations": {},
                "startsAt": "2026-08-12T10:00:00+08:00",
                "endsAt": "2026-08-12T11:00:00+08:00",
                "values": {},
                "generatorURL": "https://grafana.example.com/alert/1",
                "fingerprint": "fingerprint",
            }
        ],
    }

    webhook = parse_grafana_body(json.dumps(body).encode(), max_bytes=1_048_576)

    assert webhook.alerts[0].starts_at.tzinfo == UTC
    assert webhook.alerts[0].starts_at.isoformat() == "2026-08-12T02:00:00+00:00"


def test_parse_grafana_body_rejects_invalid_json_without_revealing_the_raw_body():
    supplied_raw_body = b'{"status": invalid, "credential": "do-not-log-me"}'

    with pytest.raises(GrafanaPayloadInvalid) as error:
        parse_grafana_body(supplied_raw_body, max_bytes=1_048_576)

    assert supplied_raw_body.decode() not in str(error.value)
    assert "do-not-log-me" not in str(error.value)


def test_parse_grafana_body_rejects_empty_alerts_and_schema_requires_one_item():
    raw_body = b'{"status":"firing","alerts":[]}'

    with pytest.raises(GrafanaPayloadInvalid):
        parse_grafana_body(raw_body, max_bytes=1_048_576)

    assert GrafanaWebhook.model_json_schema()["properties"]["alerts"]["minItems"] == 1


def test_parse_grafana_body_rejects_a_body_over_one_mebibyte_without_revealing_it():
    supplied_raw_body = b"do-not-log-me" * ((1_048_576 // len(b"do-not-log-me")) + 1)

    with pytest.raises(GrafanaPayloadTooLarge) as error:
        parse_grafana_body(supplied_raw_body, max_bytes=1_048_576)

    assert "do-not-log-me" not in str(error.value)


def test_parse_grafana_body_accepts_a_valid_body_at_exactly_one_mebibyte():
    body = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {},
                "annotations": {},
                "startsAt": "2026-08-12T02:00:00Z",
                "endsAt": "2026-08-12T03:00:00Z",
                "values": {},
                "generatorURL": "https://grafana.example.com/alert/1",
                "fingerprint": "fingerprint",
            }
        ],
    }
    raw_body = json.dumps(body, separators=(",", ":")).encode()
    raw_body += b" " * (1_048_576 - len(raw_body))

    webhook = parse_grafana_body(raw_body, max_bytes=1_048_576)

    assert len(raw_body) == 1_048_576
    assert webhook.raw_body is raw_body


def test_parse_grafana_body_rejects_naive_timestamps_without_revealing_the_raw_body():
    supplied_raw_body = b"""{
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {},
            "annotations": {},
            "startsAt": "2026-08-12T02:00:00",
            "endsAt": "2026-08-12T03:00:00Z",
            "values": {},
            "generatorURL": "https://grafana.example.com/alert/1",
            "fingerprint": "do-not-log-me"
        }]
    }"""

    with pytest.raises(GrafanaPayloadInvalid) as error:
        parse_grafana_body(supplied_raw_body, max_bytes=1_048_576)

    assert "do-not-log-me" not in str(error.value)
