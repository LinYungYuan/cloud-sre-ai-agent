from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SpecialistKind(StrEnum):
    METRICS = "metrics"
    TRACE = "trace"
    LOG = "log"


class CloudScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["GCP", "AWS"]
    scope_id: str = Field(min_length=1)
    safe: bool


def schema_hash(schema: dict[str, Any]) -> str:
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint_identity: Literal["metrics", "trace", "log"]
    capability: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    tool_name_pattern: str = Field(min_length=1)
    input_schema: dict[str, Any]
    input_schema_hash: str | None = None
    risk: Literal["READ_ONLY"]

    @model_validator(mode="after")
    def validate_manifest(self) -> ManifestEntry:
        re.compile(self.tool_name_pattern)
        Draft202012Validator.check_schema(self.input_schema)
        expected = schema_hash(self.input_schema)
        if self.input_schema_hash is not None and self.input_schema_hash != expected:
            raise ValueError("input_schema_hash does not match input_schema")
        object.__setattr__(self, "input_schema_hash", expected)
        return self


class DiscoveredTool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    input_schema: dict[str, Any]
    annotations: dict[str, Any]


class AllowedTool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    name: str
    capability: str
    endpoint_identity: Literal["metrics", "trace", "log"]
    input_schema: dict[str, Any]

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        Draft202012Validator(self.input_schema).validate(arguments)
