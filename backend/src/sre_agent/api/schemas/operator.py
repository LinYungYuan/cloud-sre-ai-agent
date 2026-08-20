from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Provider = Literal["GCP", "AWS"]
CanonicalSeverity = Literal["SEV1", "SEV3", "UNMAPPED"]
IncidentStatus = Literal["OPEN", "INVESTIGATING", "RESOLVED"]
AlertState = Literal["FIRING", "RESOLVED"]
ClassificationStatus = Literal["CLASSIFIED", "UNCLASSIFIED"]
RcaRunStatus = Literal[
    "WAITING_FOR_CLASSIFICATION",
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "PARTIAL",
    "FAILED",
    "CANCELLED",
]


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


class OperatorModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra="forbid",
    )


class Scope(OperatorModel):
    team_id: UUID | None
    project_id: UUID | None
    environment_id: UUID | None
    service_id: UUID | None


class IncidentSummary(OperatorModel):
    id: UUID
    incident_number: str
    title: str
    severity: CanonicalSeverity
    status: IncidentStatus
    alert_state: AlertState
    rca_status: RcaRunStatus | None
    provider: Provider | None
    folder_code: str | None
    alert_name: str | None
    scope: Scope
    acknowledged: bool
    acknowledged_at: datetime | None = None
    acknowledged_by: UUID | None = None
    assignee: dict[str, Any] | None = None
    opened_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    version: int = Field(ge=1)


class IncidentDetail(IncidentSummary):
    description: str
    alert_ids: list[UUID]
    rca_run_ids: list[UUID]


class CursorPageIncidents(OperatorModel):
    items: list[IncidentSummary]
    next_cursor: str | None


class AlertIssue(OperatorModel):
    raw_text: str
    source: Literal["grafana.annotations.AlertValues"]
    content_type: Literal["text/plain"]
    untrusted: Literal[True]


class NormalizationInfo(OperatorModel):
    status: str
    rule_id: UUID | None
    rule_version: int | None


class AlertDetail(OperatorModel):
    id: UUID
    source_id: UUID
    incident_id: UUID | None = None
    fingerprint: str
    title: str
    severity: CanonicalSeverity
    state: AlertState
    classification_status: ClassificationStatus
    scope: Scope | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    updated_at: datetime
    provider: Provider
    folder_code: str | None
    alert_name: str | None
    severity_raw: str | None
    issue: AlertIssue
    normalization: NormalizationInfo | None = None
    normalization_warnings: list[str]
    labels: dict[str, Any]
    annotations: dict[str, str]
    generator_url: str | None = None


class RcaRun(OperatorModel):
    id: UUID
    incident_id: UUID
    run_number: int = Field(ge=1)
    status: RcaRunStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: str | None = None
    report_id: UUID | None = None


class CursorPageRcaRuns(OperatorModel):
    items: list[RcaRun]
    next_cursor: str | None


class RcaReport(OperatorModel):
    id: UUID
    rca_run_id: UUID
    incident_id: UUID
    report_version: int = Field(ge=1)
    status: str
    summary: str
    root_cause: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    impact: str
    recommendations: list[str]
    hypotheses: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    created_at: datetime
