from __future__ import annotations

from datetime import datetime
from typing import get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sre_rca_worker.agents.specialists.base import SpecialistResult
from sre_rca_worker.domain.evidence.analysis import (
    SpecialistAnalysisDraft,
    StableSpecialistCode,
)
from sre_rca_worker.domain.evidence.models import EvidenceReference, _aware
from sre_rca_worker.integrations.mcp.models import CloudScope, SpecialistKind

_STABLE_SPECIALIST_CODES = frozenset(get_args(StableSpecialistCode))


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


class SpecialistAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis: SpecialistAnalysisDraft
    known_evidence: tuple[EvidenceReference, ...]


class SpecialistAnalysisBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    results: tuple[SpecialistAnalysisResult, ...] = ()
    failures: tuple[SpecialistFailure, ...] = ()

    @model_validator(mode="after")
    def validate_failure_codes(self) -> SpecialistAnalysisBundle:
        if any(
            failure.code not in _STABLE_SPECIALIST_CODES for failure in self.failures
        ):
            raise ValueError("analysis failures require a stable specialist code")
        return self
