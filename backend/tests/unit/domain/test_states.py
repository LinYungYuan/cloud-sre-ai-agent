from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from sre_agent.domain.alerts.models import AlertState, ClassificationStatus
from sre_agent.domain.common import EvidenceRelation, UtcTimestamp
from sre_agent.domain.incidents.models import IncidentStatus, RcaRunStatus


def test_public_enum_values_are_stable():
    assert [state.value for state in AlertState] == ["FIRING", "RESOLVED"]
    assert [status.value for status in ClassificationStatus] == [
        "CLASSIFIED",
        "UNCLASSIFIED",
    ]
    assert [status.value for status in IncidentStatus] == [
        "OPEN",
        "INVESTIGATING",
        "RESOLVED",
    ]
    assert [status.value for status in RcaRunStatus] == [
        "WAITING_FOR_CLASSIFICATION",
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "PARTIAL",
        "FAILED",
        "CANCELLED",
    ]


def test_evidence_relations_are_stable():
    assert {relation.value for relation in EvidenceRelation} == {
        "SUPPORTS",
        "CONTRADICTS",
        "MISSING",
    }


def test_utc_timestamp_normalizes_aware_datetimes_to_utc():
    timestamp = datetime(2026, 8, 12, 9, 0, tzinfo=timezone(timedelta(hours=8)))

    normalized = TypeAdapter(UtcTimestamp).validate_python(timestamp)

    assert normalized == datetime(2026, 8, 12, 1, 0, tzinfo=UTC)


def test_utc_timestamp_rejects_naive_datetimes():
    with pytest.raises(ValidationError, match="timezone-aware"):
        TypeAdapter(UtcTimestamp).validate_python(
            datetime(2026, 8, 12, 1, 0)  # noqa: DTZ001
        )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IncidentStatus.OPEN, IncidentStatus.INVESTIGATING),
        (IncidentStatus.INVESTIGATING, IncidentStatus.RESOLVED),
        (IncidentStatus.RESOLVED, IncidentStatus.OPEN),
    ],
)
def test_incident_permits_lifecycle_transitions(
    current: IncidentStatus, target: IncidentStatus
):
    assert current.can_transition_to(target)


def test_incident_rejects_direct_open_to_resolved_transition():
    assert not IncidentStatus.OPEN.can_transition_to(IncidentStatus.RESOLVED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IncidentStatus.OPEN, IncidentStatus.RESOLVED),
        (IncidentStatus.INVESTIGATING, IncidentStatus.OPEN),
        (IncidentStatus.RESOLVED, IncidentStatus.INVESTIGATING),
    ],
)
def test_incident_rejects_non_lifecycle_transitions(
    current: IncidentStatus, target: IncidentStatus
):
    assert not current.can_transition_to(target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RcaRunStatus.WAITING_FOR_CLASSIFICATION, RcaRunStatus.QUEUED),
        (RcaRunStatus.QUEUED, RcaRunStatus.RUNNING),
        (RcaRunStatus.RUNNING, RcaRunStatus.SUCCEEDED),
        (RcaRunStatus.RUNNING, RcaRunStatus.PARTIAL),
        (RcaRunStatus.RUNNING, RcaRunStatus.FAILED),
        (RcaRunStatus.RUNNING, RcaRunStatus.CANCELLED),
    ],
)
def test_rca_permits_lifecycle_transitions(
    current: RcaRunStatus, target: RcaRunStatus
):
    assert current.can_transition_to(target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RcaRunStatus.WAITING_FOR_CLASSIFICATION, RcaRunStatus.RUNNING),
        (RcaRunStatus.QUEUED, RcaRunStatus.SUCCEEDED),
        (RcaRunStatus.SUCCEEDED, RcaRunStatus.QUEUED),
        (RcaRunStatus.FAILED, RcaRunStatus.RUNNING),
    ],
)
def test_rca_rejects_non_lifecycle_transitions(
    current: RcaRunStatus, target: RcaRunStatus
):
    assert not current.can_transition_to(target)
