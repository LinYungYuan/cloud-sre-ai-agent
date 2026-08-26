from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sre_rca_worker.domain.evidence.models import EvidenceReference

_MAX_CHUNK_CHARS = 8_000
_MAX_CHUNKS = 4
_MAX_TOTAL_CHARS = 32_000


class McpPayloadTooLargeError(ValueError):
    """Raised when an MCP response exceeds its configured raw byte limit."""


class EvidenceChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: EvidenceReference
    chunk_index: int = Field(ge=0)
    chunk_count: int = Field(gt=0, le=_MAX_CHUNKS)
    content: str = Field(max_length=_MAX_CHUNK_CHARS)
    truncated: bool


def build_evidence_chunks(
    reference: EvidenceReference,
    structured_json: dict[str, Any] | list[Any],
    *,
    chunk_chars: int,
    max_chunks: int,
    max_total_chars: int,
) -> tuple[EvidenceChunk, ...]:
    """Return canonical JSON slices that fit the persisted evidence contract."""
    _validate_limits(chunk_chars, max_chunks, max_total_chars)
    canonical = json.dumps(
        structured_json,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    truncated = len(canonical) > max_total_chars
    bounded = canonical[:max_total_chars]
    chunk_count = (len(bounded) + chunk_chars - 1) // chunk_chars
    return tuple(
        EvidenceChunk(
            reference=reference,
            chunk_index=index,
            chunk_count=chunk_count,
            content=bounded[index * chunk_chars : (index + 1) * chunk_chars],
            truncated=truncated,
        )
        for index in range(chunk_count)
    )


def _validate_limits(chunk_chars: int, max_chunks: int, max_total_chars: int) -> None:
    if not 0 < chunk_chars <= _MAX_CHUNK_CHARS:
        raise ValueError("chunk_chars exceeds the persisted evidence contract")
    if not 0 < max_chunks <= _MAX_CHUNKS:
        raise ValueError("max_chunks exceeds the persisted evidence contract")
    if not 0 < max_total_chars <= _MAX_TOTAL_CHARS:
        raise ValueError("max_total_chars exceeds the persisted evidence contract")
    if max_total_chars > chunk_chars * max_chunks:
        raise ValueError("max_total_chars requires more chunks than allowed")
