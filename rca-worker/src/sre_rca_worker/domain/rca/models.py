from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sre_rca_worker.domain.evidence.models import EvidenceReference


class EvidenceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: str = Field(min_length=1)
    relation: Literal["SUPPORTS", "CONTRADICTS", "MISSING"]
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)


class RcaHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    claims: tuple[EvidenceClaim, ...] = Field(min_length=1)


class RcaReportDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["COMPLETE", "PARTIAL", "FAILED"]
    summary_zh_tw: str = Field(min_length=1)
    hypotheses: tuple[RcaHypothesis, ...]
    missing_evidence: tuple[str, ...]
    remediation: tuple[str, ...]
    verification_steps: tuple[str, ...]

    @model_validator(mode="after")
    def validate_status(self) -> RcaReportDraft:
        if self.status == "COMPLETE" and (not self.hypotheses or self.missing_evidence):
            raise ValueError(
                "COMPLETE requires a cited hypothesis and no missing evidence"
            )
        if self.status == "FAILED" and self.hypotheses:
            raise ValueError("FAILED cannot contain hypotheses")
        return self
