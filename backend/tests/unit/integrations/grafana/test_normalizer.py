import hashlib
import json
from dataclasses import replace
from uuid import UUID

import pytest

from sre_agent.domain.alerts.models import AlertState
from sre_agent.domain.alerts.normalization import (
    NormalizationRule,
    NormalizationStatus,
    RuleCondition,
    RuleOutput,
    SafeRuleEngine,
)
from sre_agent.domain.alerts.provider import Provider
from sre_agent.integrations.grafana.normalizer import normalize_alerts
from sre_agent.integrations.grafana.payloads import parse_grafana_body

SOURCE = UUID("00000000-0000-0000-0000-000000000001")
RULE_ID = UUID("10000000-0000-0000-0000-000000000001")


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


def test_normalized_nested_values_are_immutable_and_do_not_alias_the_input():
    alert = _alert(fingerprint="grafana-api", status="firing", service="api")
    alert["values"] = {"A": {"samples": [95, 96]}}
    _, webhook = _parse({"status": "firing", "alerts": [alert]})

    event = normalize_alerts(SOURCE, webhook)[0]
    original_nested = webhook.alerts[0].values["A"]
    assert isinstance(original_nested, dict)
    original_nested["samples"].append(97)

    frozen_nested = event.values["A"]
    assert frozen_nested == {"samples": (95, 96)}
    assert isinstance(frozen_nested, dict) is False
    with pytest.raises(TypeError):
        frozen_nested["samples"] = (1,)  # type: ignore[index]


def test_direct_canonical_event_construction_deep_freezes_nested_values():
    _, webhook = _parse(
        {
            "status": "firing",
            "alerts": [_alert(fingerprint="grafana-api", status="firing", service="api")],
        }
    )
    event = normalize_alerts(SOURCE, webhook)[0]
    supplied_values = {"A": {"samples": [95, 96]}}

    directly_constructed = replace(event, values=supplied_values)
    supplied_values["A"]["samples"].append(97)

    assert directly_constructed.values == {"A": {"samples": (95, 96)}}


def test_approved_alert_values_becomes_exact_untrusted_issue() -> None:
    issue = "Account: 123456789012\nValue: 85.23%\n<br>"
    alert = _alert(fingerprint="fp", status="firing", service="api")
    alert["labels"] = {
        "folder": "COM-LX-BOA-01",
        "alertname": "High CPU usage",
        "severity": "ERROR",
        "Series": "123456789012",
    }
    alert["annotations"] = {"AlertValues": issue}
    _, webhook = _parse({"status": "firing", "alerts": [alert]})

    event = normalize_alerts(SOURCE, webhook)[0]

    assert event.provider is Provider.AWS
    assert event.folder_code == "COM-LX-BOA-01"
    assert event.alert_name == "High CPU usage"
    assert event.severity.canonical == "SEV1"
    assert event.issue.raw_text == issue
    assert event.issue.source == "grafana.annotations.AlertValues"
    assert event.issue.content_type == "text/plain"
    assert event.issue.untrusted is True


def test_multiple_alerts_receive_independent_provider_and_rule_results() -> None:
    aws = _alert(fingerprint="aws", status="firing", service="api")
    aws["labels"] = {
        "folder": "COM-LX-BOA-01",
        "alertname": "High CPU usage",
        "severity": "WARNING",
        "Series": "123456789012",
    }
    aws["annotations"] = {"AlertValues": "AWS issue"}
    gcp = _alert(fingerprint="gcp", status="firing", service="worker")
    gcp["labels"] = {
        "folder": "COM-LX-BOA-01",
        "alertname": "High CPU usage",
        "severity": "ERROR",
        "resource.label.project_id": "project-123",
    }
    gcp["annotations"] = {"AlertValues": "GCP issue"}
    rules = SafeRuleEngine(
        (
            NormalizationRule(
                id=RULE_ID,
                name="aws-series",
                version=1,
                priority=1,
                conditions=(
                    RuleCondition(path="labels.Series", operator="exists"),
                ),
                output=RuleOutput(provider=Provider.AWS, resource_type="aws_resource"),
            ),
        )
    )
    _, webhook = _parse({"status": "firing", "alerts": [aws, gcp]})

    aws_event, gcp_event = normalize_alerts(SOURCE, webhook, rules)

    assert aws_event.provider is Provider.AWS
    assert aws_event.normalization_status is NormalizationStatus.NORMALIZED
    assert aws_event.normalization_rule_id == RULE_ID
    assert aws_event.issue.raw_text == "AWS issue"
    assert gcp_event.provider is Provider.GCP
    assert gcp_event.normalization_status is NormalizationStatus.UNCLASSIFIED
    assert gcp_event.issue.raw_text == "GCP issue"
