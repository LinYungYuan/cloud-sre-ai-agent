from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from ipaddress import ip_address
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
    ValidationInfo,
    field_validator,
)

_JSON_SCALAR = StrictStr | StrictInt | StrictFloat | StrictBool
_NUMBER = StrictInt | StrictFloat
_STATUS = Literal["OK", "ERROR", "UNSET"]
_KIND = Literal["INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER"]
_ALLOWED_ATTRIBUTES = frozenset(
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
_HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)
_RPC_SYSTEMS = frozenset({"grpc", "http", "jsonrpc", "aws-api"})
_DB_SYSTEMS = frozenset(
    {"postgresql", "mysql", "sqlite", "mssql", "mongodb", "redis"}
)
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SERVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HTTP_OPERATION_PATTERN = re.compile(
    r"^(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT) /[A-Za-z0-9._~:/-]{0,127}$"
)
_IDENTIFIER_OPERATION_PATTERN = re.compile(
    r"^/?[A-Za-z][A-Za-z0-9_-]{0,31}(?:[./][A-Za-z][A-Za-z0-9_-]{0,31}){0,7}$"
)
_BASE64URL_PART_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_DIAGNOSTIC_LABEL_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9_-]{0,31}(?:[./][A-Za-z][A-Za-z0-9_-]{0,31}){0,7}$"
)
_SEMANTIC_TERM_PATTERN = re.compile(r"[A-Za-z0-9]+")


class StoredTraceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=False)

    span_id: StrictStr = Field(alias="spanId", min_length=1)
    parent_span_id: StrictStr | None = Field(alias="parentSpanId")
    service_name: StrictStr = Field(alias="serviceName", min_length=1)
    operation_name: StrictStr = Field(alias="operationName", min_length=1)
    start_offset_ms: _NUMBER = Field(alias="startOffsetMs", ge=0)
    duration_ms: _NUMBER = Field(alias="durationMs", ge=0)
    status: _STATUS
    kind: _KIND
    critical_path: StrictBool = Field(alias="criticalPath")
    attributes: dict[StrictStr, _JSON_SCALAR]

    @field_validator("span_id", "parent_span_id")
    @classmethod
    def _safe_span_identifier(cls, value: str | None) -> str | None:
        if value is not None and (
            _IDENTIFIER_PATTERN.fullmatch(value) is None or _is_compact_jwt(value)
        ):
            raise ValueError("span ID must be a conventional identifier")
        return value

    @field_validator("service_name")
    @classmethod
    def _safe_service_name(cls, value: str) -> str:
        if (
            _SERVICE_NAME_PATTERN.fullmatch(value) is None
            or _is_compact_jwt(value)
        ):
            raise ValueError("serviceName must be a conventional service identifier")
        return value

    @field_validator("operation_name")
    @classmethod
    def _safe_operation_name(cls, value: str) -> str:
        if (
            _HTTP_OPERATION_PATTERN.fullmatch(value) is None
            and _IDENTIFIER_OPERATION_PATTERN.fullmatch(value) is None
        ) or _is_compact_jwt(value):
            raise ValueError("operationName must be a canonical operation label")
        return value

    @field_validator("start_offset_ms", "duration_ms", mode="before")
    @classmethod
    def _number_not_boolean(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("span time must be a number")
        return value

    @field_validator("start_offset_ms", "duration_ms")
    @classmethod
    def _finite_number(cls, value: _NUMBER) -> _NUMBER:
        if not isfinite(value):
            raise ValueError("span time must be finite")
        return value

    @field_validator("attributes")
    @classmethod
    def _allowed_scalar_attributes(
        cls,
        value: dict[str, _JSON_SCALAR],
        info: ValidationInfo,
    ) -> dict[str, _JSON_SCALAR]:
        if any(key not in _ALLOWED_ATTRIBUTES for key in value):
            raise ValueError("Trace attribute is not allowlisted")
        service_name = info.data.get("service_name")
        operation_name = info.data.get("operation_name")
        if not isinstance(service_name, str) or not isinstance(operation_name, str):
            return {}
        return _safe_attributes(
            value,
            service_name=service_name,
            operation_name=operation_name,
        )


class StoredTraceWaterfall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=False)

    schema_version: Literal[1] = Field(alias="schemaVersion")
    trace_id: StrictStr = Field(alias="traceId", min_length=1)
    root_service_name: StrictStr = Field(alias="rootServiceName", min_length=1)
    root_operation_name: StrictStr = Field(alias="rootOperationName", min_length=1)
    started_at: datetime = Field(alias="startedAt")
    duration_ms: _NUMBER = Field(alias="durationMs", ge=0)
    span_count: StrictInt = Field(alias="spanCount", ge=1)
    representative_score: _NUMBER = Field(alias="representativeScore", ge=0)
    truncated: StrictBool
    spans: list[StoredTraceSpan] = Field(min_length=1, max_length=100)

    @field_validator("trace_id")
    @classmethod
    def _safe_trace_identifier(cls, value: str) -> str:
        if _IDENTIFIER_PATTERN.fullmatch(value) is None or _is_compact_jwt(value):
            raise ValueError("trace ID must be a conventional identifier")
        return value

    @field_validator("root_service_name")
    @classmethod
    def _safe_root_service_name(cls, value: str) -> str:
        return StoredTraceSpan._safe_service_name(value)

    @field_validator("root_operation_name")
    @classmethod
    def _safe_root_operation_name(cls, value: str) -> str:
        return StoredTraceSpan._safe_operation_name(value)

    @field_validator("started_at", mode="before")
    @classmethod
    def _timestamp_must_be_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise TypeError("startedAt must be an ISO timestamp string")
        return value

    @field_validator("started_at")
    @classmethod
    def _timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("startedAt must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("duration_ms", "representative_score", mode="before")
    @classmethod
    def _trace_number_not_boolean(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("trace value must be a number")
        return value

    @field_validator("duration_ms", "representative_score")
    @classmethod
    def _finite_trace_number(cls, value: _NUMBER) -> _NUMBER:
        if not isfinite(value):
            raise ValueError("trace value must be finite")
        return value

    def has_valid_tree(self) -> bool:
        span_by_id: dict[str, StoredTraceSpan] = {}
        for span in self.spans:
            if span.span_id in span_by_id:
                return False
            span_by_id[span.span_id] = span

        roots = [span for span in self.spans if span.parent_span_id is None]
        if len(roots) != 1:
            return False
        root = roots[0]
        if (
            root.service_name != self.root_service_name
            or root.operation_name != self.root_operation_name
        ):
            return False
        if self.span_count < len(self.spans) or self.truncated != (
            self.span_count > len(self.spans)
        ):
            return False
        if any(
            span.start_offset_ms + span.duration_ms > self.duration_ms + 1
            for span in self.spans
        ):
            return False
        for span in self.spans:
            if (
                span.parent_span_id is not None
                and span.parent_span_id not in span_by_id
            ):
                return False
            seen: set[str] = set()
            current_id: str | None = span.span_id
            while current_id is not None:
                if current_id in seen:
                    return False
                seen.add(current_id)
                current_id = span_by_id[current_id].parent_span_id
        return True


def parse_trace_waterfall(value: object) -> dict[str, Any] | None:
    """Return only the strict, normalized Trace projection stored by the worker."""
    if not isinstance(value, Mapping):
        return None
    try:
        model = StoredTraceWaterfall.model_validate(value)
    except ValidationError:
        return None
    if not model.has_valid_tree():
        return None
    return model.model_dump(by_alias=False)


def _is_compact_jwt(value: str) -> bool:
    parts = value.split(".")
    if (
        len(parts) != 3
        or any(
            _BASE64URL_PART_PATTERN.fullmatch(part) is None or len(part) % 4 == 1
            for part in parts
        )
    ):
        return False
    try:
        header = _decode_base64url_json_object(parts[0])
        payload = _decode_base64url_json_object(parts[1])
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(header.get("alg"), str) and isinstance(payload, dict)


def _decode_base64url_json_object(value: str) -> dict[str, Any]:
    padding = "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(value + padding)
    parsed = json.loads(decoded)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("expected object", value, 0)
    return parsed


def _safe_attributes(
    attributes: dict[str, _JSON_SCALAR],
    *,
    service_name: str,
    operation_name: str,
) -> dict[str, _JSON_SCALAR]:
    safe: dict[str, _JSON_SCALAR] = {}
    for key, value in attributes.items():
        if key == "http.request.method":
            if value in _HTTP_METHODS:
                safe[key] = value
            continue
        if key == "http.response.status_code":
            if type(value) is int and 100 <= value <= 599:
                safe[key] = value
            continue
        if key == "rpc.system":
            if value in _RPC_SYSTEMS:
                safe[key] = value
            continue
        if key == "rpc.service":
            if _is_related_diagnostic_label(value, service_name, operation_name):
                safe[key] = value
            continue
        if key == "rpc.method":
            if _is_related_diagnostic_label(value, operation_name):
                safe[key] = value
            continue
        if key == "db.system":
            if value in _DB_SYSTEMS:
                safe[key] = value
            continue
        if key == "db.operation.name":
            if _is_related_diagnostic_label(value, operation_name):
                safe[key] = value
            continue
        if key == "server.address":
            if _is_safe_server_address(value):
                safe[key] = value
            continue
        if key == "server.port" and type(value) is int and 1 <= value <= 65535:
            safe[key] = value
    return safe


def _is_related_diagnostic_label(value: object, *related: str) -> bool:
    if (
        not isinstance(value, str)
        or _DIAGNOSTIC_LABEL_PATTERN.fullmatch(value) is None
        or _is_compact_jwt(value)
    ):
        return False
    value_terms = {term.lower() for term in _SEMANTIC_TERM_PATTERN.findall(value)}
    related_terms = {
        term.lower()
        for label in related
        for term in _SEMANTIC_TERM_PATTERN.findall(label)
    }
    return bool(value_terms) and value_terms <= related_terms


def _is_safe_server_address(value: object) -> bool:
    if (
        not isinstance(value, str)
        or "%" in value
        or _is_compact_jwt(value)
    ):
        return False
    try:
        parsed = ip_address(value)
    except ValueError:
        return False
    return str(parsed) == value
