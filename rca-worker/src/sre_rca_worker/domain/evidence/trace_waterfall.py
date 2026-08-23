from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

ALLOWED_ATTRIBUTES = frozenset(
    {
        "http.request.method",
        "http.response.status_code",
        "rpc.system",
        "rpc.service",
        "rpc.method",
        "db.system",
        "db.operation.name",
        "server.address",
        "server.port",
    }
)

_JSON_SCALAR = StrictStr | StrictInt | StrictFloat | StrictBool
_STATUS = Literal["OK", "ERROR", "UNSET"]
_KIND = Literal["INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER"]
_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9._/-]*")
_SERVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HTTP_OPERATION_PATTERN = re.compile(
    r"^(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT) /[A-Za-z0-9._~:/-]{0,127}$"
)
_IDENTIFIER_OPERATION_PATTERN = re.compile(
    r"^/?[A-Za-z][A-Za-z0-9_-]{0,31}(?:[./][A-Za-z][A-Za-z0-9_-]{0,31}){0,7}$"
)
_MAX_RESPONSE_SPANS = 100


class _InputSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: StrictStr = Field(min_length=1)
    parent_span_id: StrictStr | None = None
    service_name: StrictStr = Field(min_length=1)
    operation_name: StrictStr = Field(min_length=1)
    start_offset_ms: float = Field(ge=0)
    duration_ms: float = Field(ge=0)
    status: _STATUS
    kind: _KIND
    critical_path: StrictBool
    attributes: dict[str, _JSON_SCALAR]

    @field_validator("service_name")
    @classmethod
    def _safe_service_name(cls, value: str) -> str:
        if _SERVICE_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("serviceName must be a conventional service identifier")
        return value

    @field_validator("operation_name")
    @classmethod
    def _safe_operation_name(cls, value: str) -> str:
        if (
            _HTTP_OPERATION_PATTERN.fullmatch(value) is None
            and _IDENTIFIER_OPERATION_PATTERN.fullmatch(value) is None
        ):
            raise ValueError("operationName must be a canonical operation label")
        return value

    @field_validator("start_offset_ms", "duration_ms", mode="before")
    @classmethod
    def _numeric_span_time(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("span time must be a number")
        return value

    @field_validator("start_offset_ms", "duration_ms")
    @classmethod
    def _finite_duration(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("span time must be finite")
        return value


class _InputTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: StrictStr = Field(min_length=1)
    started_at: datetime
    latency_anomaly_score: float = Field(default=0, ge=0)
    duration_ms: float | None = Field(default=None, ge=0)
    spans: list[_InputSpan] = Field(min_length=1)

    @field_validator("latency_anomaly_score", "duration_ms", mode="before")
    @classmethod
    def _numeric_trace_time(cls, value: object) -> object:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int | float)
        ):
            raise ValueError("trace time must be a number")
        return value

    @field_validator("started_at")
    @classmethod
    def _started_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("startedAt must be timezone-aware")
        return value

    @field_validator("latency_anomaly_score", "duration_ms")
    @classmethod
    def _finite_trace_number(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("trace time must be finite")
        return value


class _NormalizedSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str
    parent_span_id: str | None
    service_name: str
    operation_name: str
    start_offset_ms: float
    duration_ms: float
    status: _STATUS
    kind: _KIND
    critical_path: bool
    attributes: dict[str, _JSON_SCALAR]

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "serviceName": self.service_name,
            "operationName": self.operation_name,
            "startOffsetMs": self.start_offset_ms,
            "durationMs": self.duration_ms,
            "status": self.status,
            "kind": self.kind,
            "criticalPath": self.critical_path,
            "attributes": self.attributes,
        }


class _CandidateTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str
    started_at: datetime
    latency_anomaly_score: float
    duration_ms: float
    root_span_id: str
    spans: tuple[_NormalizedSpan, ...]

    @property
    def span_by_id(self) -> dict[str, _NormalizedSpan]:
        return {span.span_id: span for span in self.spans}

    @property
    def has_error(self) -> bool:
        return any(span.status == "ERROR" for span in self.spans)


class _SelectedTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: _CandidateTrace
    selected_span_ids: tuple[str, ...]
    representative_score: float
    truncated: bool

    def to_storage_dict(self) -> dict[str, Any]:
        spans = self.candidate.span_by_id
        root = spans[self.candidate.root_span_id]
        return {
            "schemaVersion": 1,
            "traceId": self.candidate.trace_id,
            "rootServiceName": root.service_name,
            "rootOperationName": root.operation_name,
            "startedAt": _utc_timestamp(self.candidate.started_at),
            "durationMs": self.candidate.duration_ms,
            "spanCount": len(self.candidate.spans),
            "representativeScore": self.representative_score,
            "truncated": self.truncated,
            "spans": [spans[span_id].to_storage_dict() for span_id in self.selected_span_ids],
        }


_TRACE_ALIASES = {
    "traceId": "trace_id",
    "trace_id": "trace_id",
    "startedAt": "started_at",
    "started_at": "started_at",
    "latencyAnomalyScore": "latency_anomaly_score",
    "latency_anomaly_score": "latency_anomaly_score",
    "durationMs": "duration_ms",
    "duration_ms": "duration_ms",
    "spans": "spans",
}
_SPAN_ALIASES = {
    "spanId": "span_id",
    "span_id": "span_id",
    "parentSpanId": "parent_span_id",
    "parent_span_id": "parent_span_id",
    "serviceName": "service_name",
    "service_name": "service_name",
    "operationName": "operation_name",
    "operation_name": "operation_name",
    "startOffsetMs": "start_offset_ms",
    "start_offset_ms": "start_offset_ms",
    "durationMs": "duration_ms",
    "duration_ms": "duration_ms",
    "status": "status",
    "kind": "kind",
    "criticalPath": "critical_path",
    "critical_path": "critical_path",
    "attributes": "attributes",
}


def normalize_trace_evidence(
    payload: dict[str, Any] | list[Any],
    *,
    alert_issue: str,
    max_spans: int = _MAX_RESPONSE_SPANS,
) -> dict[str, Any] | None:
    """Safely normalize the representative trace from decoded MCP JSON."""
    if isinstance(max_spans, bool) or max_spans < 1:
        return None
    candidates = tuple(_parse_candidates(payload))
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda item: (
            item.has_error,
            item.latency_anomaly_score,
            _issue_match_score(item, alert_issue),
            item.duration_ms,
            item.trace_id,
        ),
        reverse=True,
    )
    return _truncate(ranked[0], max_spans=min(max_spans, _MAX_RESPONSE_SPANS)).to_storage_dict()


def _parse_candidates(payload: dict[str, Any] | list[Any]) -> Iterable[_CandidateTrace]:
    for raw_candidate in _candidate_objects(payload):
        candidate = _parse_candidate(raw_candidate)
        if candidate is not None:
            yield candidate


def _candidate_objects(payload: dict[str, Any] | list[Any]) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        yield from (item for item in payload if isinstance(item, Mapping))
        return
    if not isinstance(payload, Mapping):
        return
    traces = payload.get("traces")
    if isinstance(traces, list):
        yield from (item for item in traces if isinstance(item, Mapping))
        return
    data = payload.get("data")
    if isinstance(data, Mapping) and isinstance(data.get("traces"), list):
        yield from (item for item in data["traces"] if isinstance(item, Mapping))
        return
    if "spans" in payload:
        yield payload


def _parse_candidate(raw_candidate: Mapping[str, Any]) -> _CandidateTrace | None:
    try:
        trace_data = _normalize_aliases(raw_candidate, _TRACE_ALIASES)
        raw_spans = trace_data.get("spans")
        if not isinstance(raw_spans, list):
            return None
        trace_data["spans"] = [
            _normalize_aliases(raw_span, _SPAN_ALIASES)
            if isinstance(raw_span, Mapping)
            else raw_span
            for raw_span in raw_spans
        ]
        trace = _InputTrace.model_validate(trace_data)
        return _validated_candidate(trace)
    except (TypeError, ValueError, ValidationError):
        return None


def _normalize_aliases(
    raw_value: Mapping[str, Any], aliases: Mapping[str, str]
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in raw_value.items():
        normalized_key = aliases.get(key, key)
        if normalized_key in normalized:
            raise ValueError(f"duplicate alias for {normalized_key}")
        normalized[normalized_key] = value
    return normalized


def _validated_candidate(trace: _InputTrace) -> _CandidateTrace | None:
    span_by_id: dict[str, _InputSpan] = {}
    for span in trace.spans:
        if span.span_id in span_by_id:
            return None
        span_by_id[span.span_id] = span
    roots = [span for span in trace.spans if span.parent_span_id is None]
    if len(roots) != 1:
        return None
    for span in trace.spans:
        if span.parent_span_id is not None and span.parent_span_id not in span_by_id:
            return None
    if _has_cycle(span_by_id):
        return None

    derived_duration = max(
        span.start_offset_ms + span.duration_ms for span in trace.spans
    )
    duration_ms = trace.duration_ms if trace.duration_ms is not None else derived_duration
    if any(
        span.start_offset_ms + span.duration_ms > duration_ms + 1
        for span in trace.spans
    ):
        return None
    spans = tuple(
        _NormalizedSpan(
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            service_name=span.service_name,
            operation_name=span.operation_name,
            start_offset_ms=span.start_offset_ms,
            duration_ms=span.duration_ms,
            status=span.status,
            kind=span.kind,
            critical_path=span.critical_path,
            attributes={
                key: value
                for key, value in span.attributes.items()
                if key in ALLOWED_ATTRIBUTES
            },
        )
        for span in trace.spans
    )
    return _CandidateTrace(
        trace_id=trace.trace_id,
        started_at=trace.started_at,
        latency_anomaly_score=trace.latency_anomaly_score,
        duration_ms=duration_ms,
        root_span_id=roots[0].span_id,
        spans=spans,
    )


def _has_cycle(span_by_id: Mapping[str, _InputSpan]) -> bool:
    for span_id in span_by_id:
        seen: set[str] = set()
        current_id: str | None = span_id
        while current_id is not None:
            if current_id in seen:
                return True
            seen.add(current_id)
            current_id = span_by_id[current_id].parent_span_id
    return False


def _issue_match_score(candidate: _CandidateTrace, alert_issue: str) -> int:
    issue_tokens = {
        token
        for token in _TOKEN_PATTERN.findall(alert_issue.lower())
        if len(token) >= 3
    }
    if not issue_tokens:
        return 0
    trace_text = " ".join(
        f"{span.service_name} {span.operation_name}".lower()
        for span in candidate.spans
    )
    return sum(token in trace_text for token in issue_tokens)


def _truncate(candidate: _CandidateTrace, *, max_spans: int) -> _SelectedTrace:
    spans = candidate.span_by_id
    critical_ids = {span.span_id for span in candidate.spans if span.critical_path}
    error_ids = {span.span_id for span in candidate.spans if span.status == "ERROR"}
    required_ids = _ancestor_closure(spans, critical_ids | error_ids)
    required_ids.add(candidate.root_span_id)

    selected_ids: set[str]
    if len(required_ids) > max_spans:
        critical_closure = _ancestor_closure(spans, critical_ids)
        critical_closure.add(candidate.root_span_id)
        selected_ids = set(
            _topological_order(candidate, critical_closure)[:max_spans]
        )
    else:
        selected_ids = set(required_ids)
        for span_id in _stable_span_ids(candidate, set(spans)):
            _add_with_ancestors(spans, selected_ids, span_id, max_spans)

    selected_span_ids = tuple(_topological_order(candidate, selected_ids))
    representative_score = _representative_score(candidate)
    return _SelectedTrace(
        candidate=candidate,
        selected_span_ids=selected_span_ids,
        representative_score=representative_score,
        truncated=len(selected_span_ids) < len(candidate.spans),
    )


def _ancestor_closure(
    spans: Mapping[str, _NormalizedSpan], span_ids: set[str]
) -> set[str]:
    closure: set[str] = set()
    for span_id in span_ids:
        current_id: str | None = span_id
        while current_id is not None:
            closure.add(current_id)
            current_id = spans[current_id].parent_span_id
    return closure


def _add_with_ancestors(
    spans: Mapping[str, _NormalizedSpan],
    selected_ids: set[str],
    span_id: str,
    max_spans: int,
) -> None:
    chain = _ancestor_closure(spans, {span_id})
    if len(selected_ids | chain) <= max_spans:
        selected_ids.update(chain)


def _stable_span_ids(
    candidate: _CandidateTrace, span_ids: set[str]
) -> list[str]:
    spans = candidate.span_by_id
    return sorted(span_ids, key=lambda span_id: (spans[span_id].start_offset_ms, span_id))


def _topological_order(
    candidate: _CandidateTrace, selected_ids: set[str]
) -> list[str]:
    spans = candidate.span_by_id
    children: dict[str | None, list[str]] = {}
    for span_id in selected_ids:
        parent_id = spans[span_id].parent_span_id
        children.setdefault(parent_id, []).append(span_id)
    for sibling_ids in children.values():
        sibling_ids.sort(key=lambda span_id: (spans[span_id].start_offset_ms, span_id))

    ordered: list[str] = []

    def visit(span_id: str) -> None:
        ordered.append(span_id)
        for child_id in children.get(span_id, []):
            visit(child_id)

    for root_id in children.get(None, []):
        visit(root_id)
    return ordered


def _representative_score(candidate: _CandidateTrace) -> float:
    return (
        (1.0 if candidate.has_error else 0.0)
        + candidate.latency_anomaly_score
        + candidate.duration_ms / 1_000_000_000
    )


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
