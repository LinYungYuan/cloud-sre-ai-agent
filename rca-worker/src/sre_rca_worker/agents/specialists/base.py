from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sre_rca_worker.domain.evidence.models import Finding, _aware
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

    def __init__(self, client_factory: Callable[[], McpClient]) -> None:
        self._client_factory = client_factory

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
        raise NotImplementedError(
            "specialist execution adapter is configured by the workflow"
        )
