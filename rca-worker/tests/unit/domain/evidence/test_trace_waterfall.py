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


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("operationName", "SELECT secret FROM users"),
        ("operationName", '{"email":"ada@example.com","password":"secret"}'),
        ("operationName", "Bearer secret-token"),
        ("serviceName", "customer-ada@example.com"),
    ],
)
def test_does_not_store_unsafe_service_or_operation_values(
    field: str,
    unsafe_value: str,
) -> None:
    safe_trace = {
        "traceId": "safe-trace",
        "startedAt": "2026-08-23T04:20:00Z",
        "spans": [
            {
                "spanId": "safe-root",
                "parentSpanId": None,
                "serviceName": "checkout-api",
                "operationName": "GET /health",
                "startOffsetMs": 0,
                "durationMs": 100,
                "status": "OK",
                "kind": "SERVER",
                "criticalPath": True,
                "attributes": {},
            }
        ],
    }
    unsafe_span = {
        "spanId": "unsafe-root",
        "parentSpanId": None,
        "serviceName": "checkout-api",
        "operationName": "POST /checkout",
        "startOffsetMs": 0,
        "durationMs": 50,
        "status": "ERROR",
        "kind": "SERVER",
        "criticalPath": True,
        "attributes": {},
    }
    unsafe_span[field] = unsafe_value

    result = normalize_trace_evidence(
        {
            "traces": [
                safe_trace,
                {
                    "traceId": "unsafe-trace",
                    "startedAt": "2026-08-23T04:21:00Z",
                    "spans": [unsafe_span],
                },
            ]
        },
        alert_issue="checkout latency",
    )

    assert result is not None
    assert result["traceId"] == "safe-trace"
    assert unsafe_value not in repr(result)


def test_selects_trace_with_a_nested_non_critical_error() -> None:
    payload = {
        "traces": [
            {
                "traceId": "slow-ok-trace",
                "startedAt": "2026-08-23T04:20:00Z",
                "latencyAnomalyScore": 1.0,
                "spans": [
                    {
                        "spanId": "slow-ok-root",
                        "parentSpanId": None,
                        "serviceName": "checkout-api",
                        "operationName": "POST /checkout",
                        "startOffsetMs": 0,
                        "durationMs": 1000,
                        "status": "OK",
                        "kind": "SERVER",
                        "criticalPath": True,
                        "attributes": {},
                    }
                ],
            },
            {
                "traceId": "nested-error-trace",
                "startedAt": "2026-08-23T04:21:00Z",
                "spans": [
                    {
                        "spanId": "nested-root",
                        "parentSpanId": None,
                        "serviceName": "checkout-api",
                        "operationName": "POST /checkout",
                        "startOffsetMs": 0,
                        "durationMs": 100,
                        "status": "OK",
                        "kind": "SERVER",
                        "criticalPath": True,
                        "attributes": {},
                    },
                    {
                        "spanId": "nested-error",
                        "parentSpanId": "nested-root",
                        "serviceName": "inventory-service",
                        "operationName": "reserve-items",
                        "startOffsetMs": 10,
                        "durationMs": 20,
                        "status": "ERROR",
                        "kind": "CLIENT",
                        "criticalPath": False,
                        "attributes": {},
                    },
                ],
            },
        ]
    }

    result = normalize_trace_evidence(payload, alert_issue="checkout latency")

    assert result is not None
    assert result["traceId"] == "nested-error-trace"


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


def test_overflow_returns_only_parent_closed_critical_path_spans() -> None:
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
    parent_span_id = "root"
    for index in range(1, 99):
        span_id = f"critical-{index:03d}"
        spans.append(
            {
                "spanId": span_id,
                "parentSpanId": parent_span_id,
                "serviceName": "checkout-api",
                "operationName": "cache.lookup",
                "startOffsetMs": index,
                "durationMs": 1,
                "status": "OK",
                "kind": "INTERNAL",
                "criticalPath": True,
                "attributes": {},
            }
        )
        parent_span_id = span_id
    for label, offset in (("a", 500), ("b", 600)):
        parent_id = f"error-parent-{label}"
        spans.extend(
            [
                {
                    "spanId": parent_id,
                    "parentSpanId": "root",
                    "serviceName": "checkout-api",
                    "operationName": "db.pool.acquire",
                    "startOffsetMs": offset,
                    "durationMs": 10,
                    "status": "OK",
                    "kind": "INTERNAL",
                    "criticalPath": False,
                    "attributes": {},
                },
                {
                    "spanId": f"error-{label}",
                    "parentSpanId": parent_id,
                    "serviceName": "checkout-api",
                    "operationName": "db.connection.acquire",
                    "startOffsetMs": offset + 1,
                    "durationMs": 1,
                    "status": "ERROR",
                    "kind": "INTERNAL",
                    "criticalPath": False,
                    "attributes": {},
                },
            ]
        )

    result = normalize_trace_evidence(
        {"traceId": "overflow-trace", "startedAt": "2026-08-23T04:21:00Z", "spans": spans},
        alert_issue="checkout latency",
    )

    assert result is not None
    assert result["spanCount"] == 103
    assert result["truncated"] is True
    assert len(result["spans"]) == 99
    assert all(span["criticalPath"] for span in result["spans"])
    assert {"error-a", "error-b", "error-parent-a", "error-parent-b"}.isdisjoint(
        {span["spanId"] for span in result["spans"]}
    )


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
