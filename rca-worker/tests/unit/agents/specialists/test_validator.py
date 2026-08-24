from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from sre_rca_worker.agents.specialists.validator import (
    SpecialistAnalysisValidationError,
    SpecialistAnalysisValidator,
)
from sre_rca_worker.domain.evidence.analysis import (
    SpecialistAnalysisDraft,
    SpecialistObservation,
)
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.integrations.mcp.models import SpecialistKind

PARTITION = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
OWNED = EvidenceReference(
    id=UUID("00000000-0000-0000-0000-000000000001"),
    partition_timestamp=PARTITION,
)
OTHER_SPECIALIST = EvidenceReference(
    id=UUID("00000000-0000-0000-0000-000000000002"),
    partition_timestamp=PARTITION,
)


def _observation(
    reference: EvidenceReference = OWNED,
    *,
    statement: str = "CPU usage rose during the approved incident window.",
) -> SpecialistObservation:
    return SpecialistObservation(
        statement=statement,
        confidence=0.9,
        relation="SUPPORTS",
        evidence=(reference,),
    )


def _draft(
    *,
    specialist: SpecialistKind = SpecialistKind.METRICS,
    observation: SpecialistObservation | None = None,
) -> SpecialistAnalysisDraft:
    return SpecialistAnalysisDraft(
        specialist=specialist,
        status="COMPLETE",
        observations=(observation or _observation(),),
    )


def test_validator_rejects_a_draft_for_another_specialist_with_safe_code() -> None:
    with pytest.raises(SpecialistAnalysisValidationError) as raised:
        SpecialistAnalysisValidator().validate(
            _draft(specialist=SpecialistKind.LOG),
            expected_specialist=SpecialistKind.METRICS,
            owned_evidence=(OWNED,),
            input_truncated=False,
        )

    assert raised.value.code == "ANALYSIS_SCHEMA_INVALID"
    assert str(raised.value) == "ANALYSIS_SCHEMA_INVALID"


@pytest.mark.parametrize(
    ("citation", "owned_evidence"),
    [
        (
            EvidenceReference(
                id=UUID("00000000-0000-0000-0000-000000000099"),
                partition_timestamp=PARTITION,
            ),
            (OWNED,),
        ),
        (
            EvidenceReference(
                id=OWNED.id,
                partition_timestamp=PARTITION + timedelta(seconds=1),
            ),
            (OWNED,),
        ),
        (OTHER_SPECIALIST, (OWNED,)),
    ],
    ids=["unknown-uuid", "wrong-partition", "other-specialist-owner"],
)
def test_validator_requires_exact_owned_evidence_pairs(
    citation: EvidenceReference,
    owned_evidence: tuple[EvidenceReference, ...],
) -> None:
    with pytest.raises(SpecialistAnalysisValidationError) as raised:
        SpecialistAnalysisValidator().validate(
            _draft(observation=_observation(citation)),
            expected_specialist=SpecialistKind.METRICS,
            owned_evidence=owned_evidence,
            input_truncated=False,
        )

    assert raised.value.code == "ANALYSIS_UNKNOWN_EVIDENCE"
    assert str(raised.value) == "ANALYSIS_UNKNOWN_EVIDENCE"


def test_validator_downgrades_complete_truncated_input_to_partial() -> None:
    validated = SpecialistAnalysisValidator().validate(
        _draft(),
        expected_specialist=SpecialistKind.METRICS,
        owned_evidence=(OWNED,),
        input_truncated=True,
    )

    assert validated.status == "PARTIAL"
    assert validated.observations == (_observation(),)
    assert validated.missing_evidence == ("ANALYSIS_INPUT_TRUNCATED",)


def test_validator_does_not_duplicate_the_truncation_code() -> None:
    draft = SpecialistAnalysisDraft(
        specialist=SpecialistKind.METRICS,
        status="PARTIAL",
        observations=(_observation(),),
        missing_evidence=("ANALYSIS_INPUT_TRUNCATED",),
    )

    validated = SpecialistAnalysisValidator().validate(
        draft,
        expected_specialist=SpecialistKind.METRICS,
        owned_evidence=(OWNED,),
        input_truncated=True,
    )

    assert validated.missing_evidence == ("ANALYSIS_INPUT_TRUNCATED",)


def test_validator_revalidates_the_twenty_observation_domain_limit() -> None:
    observations = tuple(
        _observation(statement=f"Observation {index}") for index in range(21)
    )
    unvalidated = SpecialistAnalysisDraft.model_construct(
        specialist=SpecialistKind.METRICS,
        status="COMPLETE",
        observations=observations,
        missing_evidence=(),
    )

    with pytest.raises(SpecialistAnalysisValidationError) as raised:
        SpecialistAnalysisValidator().validate(
            unvalidated,
            expected_specialist=SpecialistKind.METRICS,
            owned_evidence=(OWNED,),
            input_truncated=False,
        )

    assert raised.value.code == "ANALYSIS_SCHEMA_INVALID"


def test_validator_revalidates_failed_status_invariants() -> None:
    unvalidated = SpecialistAnalysisDraft.model_construct(
        specialist=SpecialistKind.METRICS,
        status="FAILED",
        observations=(_observation(),),
        missing_evidence=("ANALYSIS_FAILED",),
    )

    with pytest.raises(SpecialistAnalysisValidationError) as raised:
        SpecialistAnalysisValidator().validate(
            unvalidated,
            expected_specialist=SpecialistKind.METRICS,
            owned_evidence=(OWNED,),
            input_truncated=False,
        )

    assert raised.value.code == "ANALYSIS_SCHEMA_INVALID"
