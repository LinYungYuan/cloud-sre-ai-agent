from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.integrations.mcp.models import SpecialistKind

StableSpecialistCode = Literal[
    "NO_SAFE_MCP_CAPABILITY",
    "MCP_TIMEOUT",
    "MCP_TRANSPORT",
    "MCP_PAYLOAD_TOO_LARGE",
    "MCP_RESULT_INVALID",
    "ANALYSIS_TIMEOUT",
    "ANALYSIS_SCHEMA_INVALID",
    "ANALYSIS_UNKNOWN_EVIDENCE",
    "ANALYSIS_INPUT_TRUNCATED",
    "ANALYSIS_FAILED",
]


class SpecialistObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    relation: Literal["SUPPORTS", "CONTRADICTS", "MISSING"]
    evidence: tuple[EvidenceReference, ...] = ()

    @model_validator(mode="after")
    def validate_evidence(self) -> SpecialistObservation:
        if self.relation != "MISSING" and not self.evidence:
            raise ValueError("non-MISSING observations require evidence")
        return self


class SpecialistAnalysisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    specialist: SpecialistKind
    status: Literal["COMPLETE", "PARTIAL", "FAILED"]
    observations: tuple[SpecialistObservation, ...] = Field(max_length=20)
    missing_evidence: tuple[StableSpecialistCode, ...] = ()

    @model_validator(mode="after")
    def validate_status(self) -> SpecialistAnalysisDraft:
        if self.status == "COMPLETE" and (
            not self.observations or self.missing_evidence
        ):
            raise ValueError(
                "COMPLETE requires observations and no missing evidence codes"
            )
        if self.status == "PARTIAL" and not (
            self.observations or self.missing_evidence
        ):
            raise ValueError(
                "PARTIAL requires observations or missing evidence codes"
            )
        if self.status == "FAILED" and self.observations:
            raise ValueError("FAILED cannot contain observations")
        return self


__all__ = [
    "SpecialistAnalysisDraft",
    "SpecialistObservation",
    "StableSpecialistCode",
]
