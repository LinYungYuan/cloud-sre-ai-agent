from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sre_rca_worker.domain.evidence.chunking import McpPayloadTooLargeError
from sre_rca_worker.domain.evidence.models import EvidenceDraft, Finding, _aware
from sre_rca_worker.integrations.mcp.client import McpClient
from sre_rca_worker.integrations.mcp.models import (
    AllowedTool,
    CloudScope,
    SpecialistKind,
)


class SpecialistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: UUID
    rca_run_id: UUID
    alert_issue: str = Field(min_length=1)
    scope: CloudScope | None
    window_start: datetime
    window_end: datetime
    available_tools: tuple[AllowedTool, ...]

    _times_aware = field_validator("window_start", "window_end")(_aware)

    @model_validator(mode="after")
    def validate_window(self) -> SpecialistRequest:
        if self.window_start > self.window_end:
            raise ValueError("invalid request window")
        return self


class SpecialistResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    specialist: SpecialistKind
    findings: tuple[Finding, ...] = ()
    missing_evidence: tuple[str, ...] = ()


class Specialist(Protocol):
    kind: SpecialistKind

    async def run(
        self, request: SpecialistRequest, deadline: datetime
    ) -> SpecialistResult: ...


class McpSpecialist:
    kind: SpecialistKind

    def __init__(
        self,
        client_factory: Callable[[], McpClient],
        *,
        max_response_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if max_response_bytes > 2 * 1024 * 1024:
            raise ValueError("max_response_bytes must not exceed 2 MiB")
        self._client_factory = client_factory
        self._max_response_bytes = max_response_bytes

    async def run(
        self, request: SpecialistRequest, deadline: datetime
    ) -> SpecialistResult:
        if (
            not request.available_tools
            or request.scope is None
            or request.scope.provider != "GCP"
            or not request.scope.safe
        ):
            return SpecialistResult(
                specialist=self.kind,
                missing_evidence=("NO_SAFE_MCP_CAPABILITY",),
            )
        client = self._client_factory()
        findings: list[Finding] = []
        missing: list[str] = []
        for tool in request.available_tools:
            if tool.endpoint_identity != self.kind.value:
                missing.append("ENDPOINT_CAPABILITY_MISMATCH")
                continue
            arguments = self._arguments(tool, request)
            if arguments is None:
                missing.append("UNSUPPORTED_TOOL_INPUT")
                continue
            tool.validate_arguments(arguments)
            raw = await client.call(tool.name, arguments, deadline)
            if len(raw) > self._max_response_bytes:
                raise McpPayloadTooLargeError("MCP response exceeds configured size limit")
            try:
                structured = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                structured = {"content": raw.decode("utf-8", errors="replace")}
            if not isinstance(structured, (dict, list)):
                structured = {"value": structured}
            normalized = self._normalize_structured(structured, request)
            if normalized is None:
                missing.append("INVALID_TRACE_EVIDENCE")
                continue
            structured = normalized
            encoded_input = json.dumps(
                arguments, sort_keys=True, separators=(",", ":")
            ).encode()
            evidence = EvidenceDraft(
                endpoint_identity=self.kind.value,
                capability=tool.capability,
                tool=tool.name,
                input_scope=request.scope,
                normalized_scope=request.scope,
                observed_at=request.window_end,
                request_window_start=request.window_start,
                request_window_end=request.window_end,
                window_start=request.window_start,
                window_end=request.window_end,
                structured_json=structured,
                raw_result=raw,
                content_type="application/json",
                input_sha256=hashlib.sha256(encoded_input).hexdigest(),
            )
            findings.append(
                Finding(
                    summary=f"{self.kind.value} MCP 回傳可用觀測資料",
                    confidence=0.5,
                    evidence=(evidence,),
                )
            )
        return SpecialistResult(
            specialist=self.kind,
            findings=tuple(findings),
            missing_evidence=tuple(missing),
        )

    def _normalize_structured(
        self,
        structured: dict[str, Any] | list[Any],
        request: SpecialistRequest,
    ) -> dict[str, Any] | list[Any] | None:
        return structured

    @staticmethod
    def _arguments(
        tool: AllowedTool, request: SpecialistRequest
    ) -> dict[str, object] | None:
        properties = tool.input_schema.get("properties", {})
        required_value = tool.input_schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required_value, list):
            return None
        required = set(required_value)
        candidates: dict[str, object] = {
            "project_id": request.scope.scope_id if request.scope else "",
            "projectId": request.scope.scope_id if request.scope else "",
            "scope_id": request.scope.scope_id if request.scope else "",
            "scopeId": request.scope.scope_id if request.scope else "",
            "start_time": request.window_start.isoformat(),
            "startTime": request.window_start.isoformat(),
            "end_time": request.window_end.isoformat(),
            "endTime": request.window_end.isoformat(),
            "query": request.alert_issue,
        }
        if not required <= candidates.keys():
            return None
        return {name: candidates[name] for name in properties if name in candidates}
