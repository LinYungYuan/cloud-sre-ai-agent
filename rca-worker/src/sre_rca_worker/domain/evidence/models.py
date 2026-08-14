from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBytes,
    field_validator,
    model_validator,
)

from sre_rca_worker.integrations.mcp.models import CloudScope


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    partition_timestamp: datetime

    _partition_aware = field_validator("partition_timestamp")(_aware)


class EvidenceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint_identity: Literal["metrics", "trace", "log"]
    capability: str
    tool: str
    input_scope: CloudScope
    normalized_scope: CloudScope
    observed_at: datetime
    request_window_start: datetime
    request_window_end: datetime
    window_start: datetime
    window_end: datetime
    structured_json: dict[str, Any] | list[Any]
    raw_result: StrictBytes
    content_type: str = Field(min_length=1, max_length=255)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    _times_aware = field_validator(
        "observed_at",
        "request_window_start",
        "request_window_end",
        "window_start",
        "window_end",
    )(_aware)

    @model_validator(mode="after")
    def validate_provenance(self) -> EvidenceDraft:
        if not self.input_scope.safe or self.input_scope.provider != "GCP":
            raise ValueError("evidence requires a safe GCP scope")
        if self.input_scope != self.normalized_scope:
            raise ValueError("evidence scope differs from normalized scope")
        if not self.capability.startswith(f"{self.endpoint_identity}."):
            raise ValueError("capability and endpoint do not match")
        if not self.tool.startswith(self.endpoint_identity):
            raise ValueError("tool and endpoint do not match")
        if not (
            self.request_window_start
            <= self.window_start
            <= self.window_end
            <= self.request_window_end
        ):
            raise ValueError("evidence window exceeds request window")
        return self


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    evidence: tuple[EvidenceDraft, ...] = Field(min_length=1)
