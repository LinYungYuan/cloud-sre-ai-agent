from enum import StrEnum


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"

    def can_transition_to(self, target: "IncidentStatus") -> bool:
        return target in _INCIDENT_TRANSITIONS[self]


class RcaRunStatus(StrEnum):
    WAITING_FOR_CLASSIFICATION = "WAITING_FOR_CLASSIFICATION"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    def can_transition_to(self, target: "RcaRunStatus") -> bool:
        return target in _RCA_RUN_TRANSITIONS[self]


_INCIDENT_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.OPEN: frozenset({IncidentStatus.INVESTIGATING}),
    IncidentStatus.INVESTIGATING: frozenset({IncidentStatus.RESOLVED}),
    IncidentStatus.RESOLVED: frozenset({IncidentStatus.OPEN}),
}

_RCA_RUN_TRANSITIONS: dict[RcaRunStatus, frozenset[RcaRunStatus]] = {
    RcaRunStatus.WAITING_FOR_CLASSIFICATION: frozenset({RcaRunStatus.QUEUED}),
    RcaRunStatus.QUEUED: frozenset({RcaRunStatus.RUNNING}),
    RcaRunStatus.RUNNING: frozenset(
        {
            RcaRunStatus.SUCCEEDED,
            RcaRunStatus.PARTIAL,
            RcaRunStatus.FAILED,
            RcaRunStatus.CANCELLED,
        }
    ),
    RcaRunStatus.SUCCEEDED: frozenset(),
    RcaRunStatus.PARTIAL: frozenset(),
    RcaRunStatus.FAILED: frozenset(),
    RcaRunStatus.CANCELLED: frozenset(),
}
