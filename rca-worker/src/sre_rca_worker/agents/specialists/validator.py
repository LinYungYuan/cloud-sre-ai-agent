from __future__ import annotations

from typing import Literal

from pydantic import ValidationError

from sre_rca_worker.domain.evidence.analysis import SpecialistAnalysisDraft
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.integrations.mcp.models import SpecialistKind

SpecialistValidationCode = Literal[
    "ANALYSIS_SCHEMA_INVALID",
    "ANALYSIS_UNKNOWN_EVIDENCE",
]


class SpecialistAnalysisValidationError(ValueError):
    """A model-output validation failure with no untrusted error detail."""

    def __init__(self, code: SpecialistValidationCode) -> None:
        self.code: SpecialistValidationCode = code
        super().__init__(code)


class SpecialistAnalysisValidator:
    def validate(
        self,
        draft: SpecialistAnalysisDraft,
        *,
        expected_specialist: SpecialistKind,
        owned_evidence: tuple[EvidenceReference, ...],
        input_truncated: bool,
    ) -> SpecialistAnalysisDraft:
        try:
            validated = SpecialistAnalysisDraft.model_validate(
                draft.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise SpecialistAnalysisValidationError("ANALYSIS_SCHEMA_INVALID") from None

        if validated.specialist is not expected_specialist:
            raise SpecialistAnalysisValidationError("ANALYSIS_SCHEMA_INVALID")

        owned = {
            (reference.id, reference.partition_timestamp)
            for reference in owned_evidence
        }
        if any(
            (reference.id, reference.partition_timestamp) not in owned
            for observation in validated.observations
            for reference in observation.evidence
        ):
            raise SpecialistAnalysisValidationError("ANALYSIS_UNKNOWN_EVIDENCE")

        if input_truncated and "ANALYSIS_INPUT_TRUNCATED" not in (
            validated.missing_evidence
        ):
            validated = validated.model_copy(
                update={
                    "status": (
                        "PARTIAL"
                        if validated.status == "COMPLETE"
                        else validated.status
                    ),
                    "missing_evidence": (
                        *validated.missing_evidence,
                        "ANALYSIS_INPUT_TRUNCATED",
                    ),
                }
            )
        return validated


__all__ = [
    "SpecialistAnalysisValidationError",
    "SpecialistAnalysisValidator",
    "SpecialistValidationCode",
]
