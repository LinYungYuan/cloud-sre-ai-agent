from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from sre_rca_worker.domain.evidence.analysis import (
    SpecialistAnalysisDraft,
    SpecialistObservation,
)
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.integrations.mcp.models import SpecialistKind

_EVIDENCE_REFERENCE = EvidenceReference(
    id=UUID("00000000-0000-0000-0000-000000000001"),
)
_STABLE_CODES = (
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
)


def _observation(
    relation: str = "SUPPORTS",
    *,
    evidence: tuple[EvidenceReference, ...] = (_EVIDENCE_REFERENCE,),
    statement: str = "The latency increased during the incident window.",
    confidence: float = 0.8,
) -> SpecialistObservation:
    return SpecialistObservation(
        statement=statement,
        confidence=confidence,
        relation=cast(Literal["SUPPORTS", "CONTRADICTS", "MISSING"], relation),
        evidence=evidence,
    )


def _draft(**overrides: object) -> SpecialistAnalysisDraft:
    values: dict[str, object] = {
        "specialist": SpecialistKind.METRICS,
        "status": "COMPLETE",
        "observations": (_observation(),),
    }
    values.update(overrides)
    return SpecialistAnalysisDraft.model_validate(values)


def test_complete_requires_an_observation() -> None:
    with pytest.raises(ValidationError, match="COMPLETE"):
        _draft(observations=())


def test_complete_rejects_missing_evidence_codes() -> None:
    with pytest.raises(ValidationError, match="COMPLETE"):
        _draft(missing_evidence=("MCP_TIMEOUT",))


def test_partial_requires_observation_or_missing_evidence() -> None:
    with pytest.raises(ValidationError, match="PARTIAL"):
        _draft(status="PARTIAL", observations=(), missing_evidence=())


def test_partial_allows_observation_without_missing_evidence() -> None:
    draft = _draft(status="PARTIAL")

    assert draft.status == "PARTIAL"
    assert draft.observations == (_observation(),)


def test_partial_allows_missing_evidence_without_observation() -> None:
    draft = _draft(
        status="PARTIAL",
        observations=(),
        missing_evidence=("MCP_TIMEOUT",),
    )

    assert draft.observations == ()
    assert draft.missing_evidence == ("MCP_TIMEOUT",)


def test_failed_rejects_observations() -> None:
    with pytest.raises(ValidationError, match="FAILED"):
        _draft(status="FAILED")


@pytest.mark.parametrize("relation", ["SUPPORTS", "CONTRADICTS"])
def test_non_missing_observation_requires_a_citation(relation: str) -> None:
    with pytest.raises(ValidationError, match="evidence"):
        _observation(relation, evidence=())


def test_missing_observation_may_have_no_citation() -> None:
    observation = _observation("MISSING", evidence=())

    assert observation.evidence == ()


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidence_must_be_between_zero_and_one(confidence: float) -> None:
    with pytest.raises(ValidationError, match="confidence"):
        _observation(confidence=confidence)


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_confidence_accepts_both_bounds(confidence: float) -> None:
    assert _observation(confidence=confidence).confidence == confidence


@pytest.mark.parametrize("code", _STABLE_CODES)
def test_accepts_only_declared_stable_codes(code: str) -> None:
    draft = _draft(status="PARTIAL", observations=(), missing_evidence=(code,))

    assert draft.missing_evidence == (code,)


def test_rejects_unknown_stable_code() -> None:
    with pytest.raises(ValidationError, match="missing_evidence"):
        _draft(status="PARTIAL", observations=(), missing_evidence=("UNKNOWN",))


@pytest.mark.parametrize("field", ["remediation", "root_cause"])
def test_rejects_remediation_and_root_cause_fields(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        _draft(**{field: "not allowed"})


def test_rejects_extra_observation_fields() -> None:
    with pytest.raises(ValidationError, match="root_cause"):
        SpecialistObservation.model_validate(
            {
                "statement": "The latency increased.",
                "confidence": 0.8,
                "relation": "SUPPORTS",
                "evidence": (_EVIDENCE_REFERENCE,),
                "root_cause": "not allowed",
            }
        )


def test_allows_at_most_twenty_observations() -> None:
    observations = tuple(
        _observation(
            "MISSING",
            evidence=(),
            statement=f"Observation {index}",
        )
        for index in range(20)
    )

    draft = _draft(observations=observations)

    assert len(draft.observations) == 20


def test_rejects_more_than_twenty_observations() -> None:
    observations = tuple(
        _observation(
            "MISSING",
            evidence=(),
            statement=f"Observation {index}",
        )
        for index in range(21)
    )

    with pytest.raises(ValidationError, match="observations"):
        _draft(observations=observations)


def test_analysis_models_are_frozen() -> None:
    draft = _draft()
    observation = draft.observations[0]

    with pytest.raises(ValidationError):
        draft.status = "PARTIAL"
    with pytest.raises(ValidationError):
        observation.statement = "changed"


def test_evidence_reference_is_uuid_only_and_rejects_partition_helpers() -> None:
    assert _EVIDENCE_REFERENCE.model_dump() == {"id": _EVIDENCE_REFERENCE.id}

    with pytest.raises(ValidationError, match="partition_timestamp"):
        EvidenceReference.model_validate(
            {
                "id": str(_EVIDENCE_REFERENCE.id),
                "partition_timestamp": "2026-08-24T08:00:00Z",
            }
        )
