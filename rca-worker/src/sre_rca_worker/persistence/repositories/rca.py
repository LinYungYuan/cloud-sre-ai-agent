from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sre_rca_worker.domain.evidence.models import EvidenceDraft, EvidenceReference


@dataclass(frozen=True, slots=True)
class PersistedEvidence:
    reference: EvidenceReference
    rca_run_id: UUID
    specialist_run_id: UUID
    evidence_type: str
    source_endpoint: str
    tool_name: str
    structured_data: dict[str, Any] | list[Any]
    metadata: dict[str, Any]


class AmbiguousEvidenceError(LookupError):
    """Raised when a partitioned evidence UUID is not unique in its owner."""


class RcaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_evidence(
        self,
        rca_run_id: UUID,
        specialist_run_id: UUID | None,
        draft: EvidenceDraft,
    ) -> EvidenceReference:
        metadata = {
            "endpointIdentity": draft.endpoint_identity,
            "capability": draft.capability,
            "scope": {
                "provider": draft.input_scope.provider,
                "scopeId": draft.input_scope.scope_id,
            },
            "contentType": draft.content_type,
            "inputSha256": draft.input_sha256,
        }
        row = (
            await self._session.execute(
                text(
                    """INSERT INTO evidence_records (
                         partition_timestamp, observed_at, rca_run_id,
                         specialist_run_id, evidence_type, source_agent,
                         source_endpoint, tool_name, time_window_start,
                         time_window_end, structured_data, raw_result,
                         metadata, content_hash
                       ) VALUES (
                         :partition_timestamp, :observed_at, :rca_run_id,
                         :specialist_run_id, :evidence_type, :source_agent,
                         :source_endpoint, :tool_name, :window_start,
                         :window_end, CAST(:structured_data AS JSONB), :raw_result,
                         CAST(:metadata AS JSONB), :content_hash
                       ) RETURNING id, partition_timestamp"""
                ),
                {
                    "partition_timestamp": draft.observed_at,
                    "observed_at": draft.observed_at,
                    "rca_run_id": rca_run_id,
                    "specialist_run_id": specialist_run_id,
                    "evidence_type": draft.capability,
                    "source_agent": draft.endpoint_identity.upper(),
                    "source_endpoint": draft.endpoint_identity,
                    "tool_name": draft.tool,
                    "window_start": draft.window_start,
                    "window_end": draft.window_end,
                    "structured_data": __import__("json").dumps(
                        draft.structured_json, ensure_ascii=False
                    ),
                    "raw_result": draft.raw_result,
                    "metadata": __import__("json").dumps(metadata),
                    "content_hash": hashlib.sha256(draft.raw_result).hexdigest(),
                },
            )
        ).one()
        return EvidenceReference(id=row.id, partition_timestamp=row.partition_timestamp)

    async def list_specialist_evidence(
        self, rca_run_id: UUID, specialist_run_id: UUID
    ) -> tuple[PersistedEvidence, ...]:
        rows = (
            (
                await self._session.execute(
                    text(
                        """SELECT id, partition_timestamp, rca_run_id,
                                  specialist_run_id, evidence_type,
                                  source_endpoint, tool_name, structured_data,
                                  metadata
                           FROM evidence_records
                           WHERE rca_run_id=:rca_run_id
                             AND specialist_run_id=:specialist_run_id
                           ORDER BY observed_at, partition_timestamp, id"""
                    ),
                    {
                        "rca_run_id": rca_run_id,
                        "specialist_run_id": specialist_run_id,
                    },
                )
            )
            .mappings()
            .all()
        )
        persisted = tuple(self._persisted_evidence(row) for row in rows)
        seen: set[UUID] = set()
        for record in persisted:
            if record.reference.id in seen:
                raise AmbiguousEvidenceError("partitioned evidence UUID is ambiguous")
            seen.add(record.reference.id)
        return persisted

    async def get_specialist_evidence(
        self,
        rca_run_id: UUID,
        specialist_run_id: UUID,
        evidence_id: UUID,
    ) -> PersistedEvidence | None:
        rows = (
            (
                await self._session.execute(
                    text(
                        """SELECT id, partition_timestamp, rca_run_id,
                                  specialist_run_id, evidence_type,
                                  source_endpoint, tool_name, structured_data,
                                  metadata
                           FROM evidence_records
                           WHERE rca_run_id=:rca_run_id
                             AND specialist_run_id=:specialist_run_id
                             AND id=:evidence_id
                           ORDER BY partition_timestamp"""
                    ),
                    {
                        "rca_run_id": rca_run_id,
                        "specialist_run_id": specialist_run_id,
                        "evidence_id": evidence_id,
                    },
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > 1:
            raise AmbiguousEvidenceError("partitioned evidence UUID is ambiguous")
        return None if not rows else self._persisted_evidence(rows[0])

    @staticmethod
    def _persisted_evidence(row: Any) -> PersistedEvidence:
        structured_data = row["structured_data"]
        metadata = row["metadata"]
        if not isinstance(structured_data, (dict, list)):
            raise TypeError("persisted evidence must contain structured JSON")
        if not isinstance(metadata, dict):
            raise TypeError("persisted evidence metadata must be an object")
        specialist_run_id = row["specialist_run_id"]
        if not isinstance(specialist_run_id, UUID):
            raise TypeError("persisted evidence must belong to a specialist run")
        partition_timestamp = row["partition_timestamp"]
        if not isinstance(partition_timestamp, datetime):
            raise TypeError("persisted evidence must have a partition timestamp")
        return PersistedEvidence(
            reference=EvidenceReference(
                id=row["id"], partition_timestamp=partition_timestamp
            ),
            rca_run_id=row["rca_run_id"],
            specialist_run_id=specialist_run_id,
            evidence_type=row["evidence_type"],
            source_endpoint=row["source_endpoint"],
            tool_name=row["tool_name"],
            structured_data=structured_data,
            metadata=metadata,
        )
