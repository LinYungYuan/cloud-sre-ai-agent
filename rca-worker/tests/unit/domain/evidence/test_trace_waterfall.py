from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from sre_rca_worker.domain.evidence.trace_waterfall import normalize_trace_evidence


def test_normalizes_and_selects_error_trace_without_sensitive_attributes() -> None:
    payload: dict[str, Any] = {
        "traces": [
            {
                "traceId": "ok-trace",
                "startedAt": "2026-08-23T04:20:00Z",
                "spans": [
                    {
                        "spanId": "ok-root",
                        "parentSpanId": None,
                        "serviceName": "checkout-api",
                        "operationName": "POST /checkout",
                        "startOffsetMs": 0,
                        "durationMs": 220,
                        "status": "OK",
                        "kind": "SERVER",
                        "criticalPath": True,
                        "attributes": {"http.request.method": "POST"},
                    }
                ],
            },
            {
                "traceId": "error-trace",
                "startedAt": "2026-08-23T04:21:00Z",
                "latencyAnomalyScore": 0.96,
                "spans": [
                    {
                        "spanId": "db",
                        "parentSpanId": "root",
                        "serviceName": "checkout-api",
                        "operationName": "db.connection.acquire",
                        "startOffsetMs": 320,
                        "durationMs": 1480,
                        "status": "ERROR",
                        "kind": "INTERNAL",
                        "criticalPath": True,
                        "attributes": {
                            "db.system": "postgresql",
                            "db.statement": "SELECT secret FROM users",
                            "authorization": "Bearer secret",
                        },
                    },
                    {
                        "spanId": "root",
                        "parentSpanId": None,
                        "serviceName": "checkout-api",
                        "operationName": "POST /checkout",
                        "startOffsetMs": 0,
                        "durationMs": 1925,
                        "status": "ERROR",
                        "kind": "SERVER",
                        "criticalPath": True,
                        "attributes": {"http.response.status_code": 500},
                    },
                ],
            },
        ]
    }

    result = normalize_trace_evidence(payload, alert_issue="checkout latency")

    assert result is not None
    assert set(result) == {
        "schemaVersion",
        "traceId",
        "rootServiceName",
        "rootOperationName",
        "startedAt",
        "durationMs",
        "spanCount",
        "representativeScore",
        "truncated",
        "spans",
    }
    assert result["traceId"] == "error-trace"
    assert [span["spanId"] for span in result["spans"]] == ["root", "db"]
    assert result["spans"][1]["attributes"] == {"db.system": "postgresql"}


def test_truncation_keeps_a_late_error_span_and_its_ancestors() -> None:
    spans = [
        {
            "spanId": "root",
            "parentSpanId": None,
            "serviceName": "checkout-api",
            "operationName": "POST /checkout",
            "startOffsetMs": 0,
            "durationMs": 2000,
            "status": "OK",
            "kind": "SERVER",
            "criticalPath": True,
            "attributes": {},
        }
    ]
    spans.extend(
        {
            "spanId": f"noise-{index:03d}",
            "parentSpanId": "root",
            "serviceName": "checkout-api",
            "operationName": "cache.lookup",
            "startOffsetMs": index,
            "durationMs": 1,
            "status": "OK",
            "kind": "INTERNAL",
            "criticalPath": False,
            "attributes": {},
        }
        for index in range(1, 103)
    )
    spans.append(
        {
            "spanId": "error-parent",
            "parentSpanId": "root",
            "serviceName": "checkout-api",
            "operationName": "db.pool.acquire",
            "startOffsetMs": 1800,
            "durationMs": 100,
            "status": "OK",
            "kind": "INTERNAL",
            "criticalPath": False,
            "attributes": {},
        }
    )
    spans.append(
        {
            "spanId": "late-error",
            "parentSpanId": "error-parent",
            "serviceName": "checkout-api",
            "operationName": "db.connection.acquire",
            "startOffsetMs": 1850,
            "durationMs": 50,
            "status": "ERROR",
            "kind": "INTERNAL",
            "criticalPath": False,
            "attributes": {},
        }
    )

    result = normalize_trace_evidence(
        {"traceId": "long-trace", "startedAt": "2026-08-23T04:21:00Z", "spans": spans},
        alert_issue="checkout latency",
        max_spans=100,
    )

    assert result is not None
    assert result["spanCount"] == 105
    assert result["truncated"] is True
    assert len(result["spans"]) == 100
    assert [span["spanId"] for span in result["spans"]][:1] == ["root"]
    retained_ids = {span["spanId"] for span in result["spans"]}
    assert {"root", "error-parent", "late-error"} <= retained_ids


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        (
            "negative duration",
            lambda payload: payload["spans"][0].update({"durationMs": -1}),
        ),
        (
            "duplicate span id",
            lambda payload: payload["spans"].append(deepcopy(payload["spans"][0])),
        ),
        (
            "missing parent",
            lambda payload: payload["spans"][0].update({"parentSpanId": "missing"}),
        ),
        (
            "unsupported status",
            lambda payload: payload["spans"][0].update({"status": "CANCELLED"}),
        ),
        (
            "non-object attributes",
            lambda payload: payload["spans"][0].update({"attributes": ["not", "an", "object"]}),
        ),
    ],
)
def test_rejects_untrusted_span_shapes(
    name: str,
    mutate: Any,
) -> None:
    payload: dict[str, Any] = {
        "traceId": "invalid-trace",
        "startedAt": "2026-08-23T04:21:00Z",
        "spans": [
            {
                "spanId": "root",
                "parentSpanId": None,
                "serviceName": "checkout-api",
                "operationName": "POST /checkout",
                "startOffsetMs": 0,
                "durationMs": 10,
                "status": "OK",
                "kind": "SERVER",
                "criticalPath": True,
                "attributes": {},
            }
        ],
    }
    mutate(payload)

    assert normalize_trace_evidence(payload, alert_issue=name) is None
