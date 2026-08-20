from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_RCA_JOB_MESSAGE_BYTES = 1024


class RcaJobMessage(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_version: Literal[1] = Field(alias="schemaVersion")
    worker_job_id: UUID = Field(alias="workerJobId")
    rca_run_id: UUID = Field(alias="rcaRunId")
    incident_id: UUID = Field(alias="incidentId")
    attempt: Literal[1]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        return cls.model_validate(payload)

    @classmethod
    def from_bytes(cls, payload: bytes) -> Self:
        if len(payload) > MAX_RCA_JOB_MESSAGE_BYTES:
            raise ValueError("RCA job message exceeds 1024 bytes")
        return cls.model_validate_json(payload)

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(by_alias=True, mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
