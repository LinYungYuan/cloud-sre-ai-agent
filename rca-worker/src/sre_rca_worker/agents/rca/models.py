from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sre_rca_worker.agents.specialists.base import SpecialistResult
from sre_rca_worker.domain.evidence.models import _aware
from sre_rca_worker.integrations.mcp.models import CloudScope, SpecialistKind


class IncidentContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: UUID
    rca_run_id: UUID
    alert_issue: str = Field(min_length=1)
    scope: CloudScope | None
    window_start: datetime
    window_end: datetime

    _times_aware = field_validator("window_start", "window_end")(_aware)

    @model_validator(mode="after")
    def validate_window(self) -> IncidentContext:
        if self.window_start > self.window_end:
            raise ValueError("invalid Incident window")
        return self


class SpecialistFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    specialist: SpecialistKind
    code: str


class InvestigationBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    results: tuple[SpecialistResult, ...] = ()
    failures: tuple[SpecialistFailure, ...] = ()
