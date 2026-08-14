from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sre_rca_worker.domain.evidence.models import EvidenceDraft, EvidenceReference


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
