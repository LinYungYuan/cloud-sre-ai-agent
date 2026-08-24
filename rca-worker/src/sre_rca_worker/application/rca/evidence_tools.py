from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sre_rca_worker.agents.specialists.base import (
    SpecialistRequest,
    SpecialistResult,
)
from sre_rca_worker.application.rca.persist_evidence import PersistEvidence
from sre_rca_worker.domain.evidence.analysis import StableSpecialistCode
from sre_rca_worker.domain.evidence.chunking import (
    EvidenceChunk,
    McpPayloadTooLargeError,
    build_evidence_chunks,
)
from sre_rca_worker.domain.evidence.models import EvidenceDraft, EvidenceReference
from sre_rca_worker.integrations.mcp.models import AllowedTool, SpecialistKind
from sre_rca_worker.persistence.repositories.rca import PersistedEvidence

_MAX_MCP_CALLS = 5
_MAX_CHUNK_CHARS = 8_000
_MAX_CHUNKS = 4
_MAX_TOTAL_CHARS = 32_000


class EvidenceCollector(Protocol):
    kind: SpecialistKind

    async def run(
        self, request: SpecialistRequest, deadline: datetime
    ) -> SpecialistResult: ...


class EvidenceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    specialist: SpecialistKind
    references: tuple[EvidenceReference, ...]
    first_chunks: tuple[EvidenceChunk, ...]
    total_chunks: int
    truncated: bool


class EvidenceToolError(RuntimeError):
    """Safe application-boundary failure exposed to an agent tool adapter."""

    def __init__(self, code: StableSpecialistCode) -> None:
        self.code = code
        super().__init__(code)


class EvidenceToolSession:
    def __init__(
        self,
        *,
        request: SpecialistRequest,
        specialist_run_id: UUID,
        collector: EvidenceCollector,
        sessions: async_sessionmaker[AsyncSession],
        deadline: datetime,
        chunk_chars: int,
        max_chunks: int,
        max_total_chars: int,
        max_tool_calls: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")
        if not 0 < max_tool_calls <= 5:
            raise ValueError("max_tool_calls must be between 1 and 5")
        if not 0 < chunk_chars <= _MAX_CHUNK_CHARS:
            raise ValueError("chunk_chars exceeds the evidence contract")
        if not 0 < max_chunks <= _MAX_CHUNKS:
            raise ValueError("max_chunks exceeds the evidence contract")
        if not 0 < max_total_chars <= _MAX_TOTAL_CHARS:
            raise ValueError("max_total_chars exceeds the evidence contract")
        if max_total_chars > chunk_chars * max_chunks:
            raise ValueError("max_total_chars exceeds chunk capacity")
        if not isinstance(collector.kind, SpecialistKind):
            raise TypeError("collector must be bound to a specialist kind")

        self._request = request
        self._specialist_run_id = specialist_run_id
        self._collector = collector
        self._sessions = sessions
        self._deadline = deadline
        self._chunk_chars = chunk_chars
        self._max_chunks = max_chunks
        self._max_total_chars = max_total_chars
        self._max_tool_calls = max_tool_calls
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._tool_calls = 0
        self._receipt: EvidenceReceipt | None = None

    async def collect_evidence(self) -> EvidenceReceipt:
        async with self._lock:
            self._admit_tool_call()
            if self._receipt is not None:
                return self._receipt
            try:
                persisted = await self._list_persisted()
                if persisted:
                    self._receipt = self._build_receipt(persisted)
                    return self._receipt

                approved_tools = self._approved_tools()
                if approved_tools is None:
                    self._receipt = self._empty_receipt()
                    return self._receipt

                bounded_request = self._request.model_copy(
                    update={"available_tools": approved_tools}
                )
                remaining = self._remaining_seconds()
                async with asyncio.timeout(remaining):
                    result = await self._collector.run(
                        bounded_request, self._deadline
                    )
                self._ensure_before_deadline()
                drafts = self._validated_drafts(result, approved_tools)
                if not drafts:
                    self._receipt = self._empty_receipt()
                    return self._receipt

                references = await self._persist_all(drafts)
                committed = tuple(
                    self._record_from_committed(reference, draft)
                    for reference, draft in zip(references, drafts, strict=True)
                )
                self._receipt = self._build_receipt(committed)
                return self._receipt
            except EvidenceToolError:
                raise
            except TimeoutError:
                raise EvidenceToolError("ANALYSIS_TIMEOUT") from None
            except McpPayloadTooLargeError:
                raise EvidenceToolError("MCP_PAYLOAD_TOO_LARGE") from None
            except (ConnectionError, OSError):
                raise EvidenceToolError("MCP_TRANSPORT") from None
            except Exception:  # noqa: BLE001 - stable public failure boundary
                raise EvidenceToolError("ANALYSIS_FAILED") from None

    async def read_evidence_chunk(
        self, evidence_id: UUID, chunk_index: int
    ) -> EvidenceChunk:
        async with self._lock:
            self._admit_tool_call()
            if not isinstance(evidence_id, UUID) or isinstance(evidence_id, bool):
                raise EvidenceToolError("ANALYSIS_UNKNOWN_EVIDENCE")
            if not isinstance(chunk_index, int) or isinstance(chunk_index, bool):
                raise EvidenceToolError("ANALYSIS_UNKNOWN_EVIDENCE")
            if chunk_index < 0:
                raise EvidenceToolError("ANALYSIS_UNKNOWN_EVIDENCE")
            try:
                async with self._sessions() as session:
                    persisted = await PersistEvidence(
                        session
                    ).get_specialist_evidence(
                        self._request.rca_run_id,
                        self._specialist_run_id,
                        evidence_id,
                    )
                if persisted is None:
                    raise EvidenceToolError("ANALYSIS_UNKNOWN_EVIDENCE")
                self._ensure_record_owned(persisted)
                chunks = self._chunks(persisted)
                if chunk_index >= len(chunks):
                    raise EvidenceToolError("ANALYSIS_UNKNOWN_EVIDENCE")
                return chunks[chunk_index]
            except EvidenceToolError:
                raise
            except Exception:  # noqa: BLE001 - stable public failure boundary
                raise EvidenceToolError("ANALYSIS_FAILED") from None

    @property
    def known_evidence(self) -> tuple[EvidenceReference, ...]:
        return () if self._receipt is None else self._receipt.references

    def _admit_tool_call(self) -> None:
        self._ensure_before_deadline()
        if self._tool_calls >= self._max_tool_calls:
            raise EvidenceToolError("ANALYSIS_FAILED")
        self._tool_calls += 1

    def _ensure_before_deadline(self) -> None:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise EvidenceToolError("ANALYSIS_FAILED")
        if now >= self._deadline:
            raise EvidenceToolError("ANALYSIS_TIMEOUT")

    def _remaining_seconds(self) -> float:
        remaining = (self._deadline - self._clock()).total_seconds()
        if remaining <= 0:
            raise EvidenceToolError("ANALYSIS_TIMEOUT")
        return remaining

    async def _list_persisted(self) -> tuple[PersistedEvidence, ...]:
        async with self._sessions() as session:
            return await PersistEvidence(session).list_specialist_evidence(
                self._request.rca_run_id, self._specialist_run_id
            )

    def _approved_tools(self) -> tuple[AllowedTool, ...] | None:
        scope = self._request.scope
        if scope is None or scope.provider != "GCP" or not scope.safe:
            return None
        approved = tuple(
            tool
            for tool in self._request.available_tools
            if tool.endpoint_identity == self._collector.kind.value
            and tool.capability.startswith(f"{self._collector.kind.value}.")
        )[:_MAX_MCP_CALLS]
        return approved or None

    def _validated_drafts(
        self,
        result: SpecialistResult,
        approved_tools: tuple[AllowedTool, ...],
    ) -> tuple[EvidenceDraft, ...]:
        if result.specialist is not self._collector.kind:
            raise EvidenceToolError("ANALYSIS_FAILED")
        allowed = {(tool.name, tool.capability) for tool in approved_tools}
        drafts = tuple(
            draft for finding in result.findings for draft in finding.evidence
        )
        scope = self._request.scope
        for draft in drafts:
            if (
                draft.endpoint_identity != self._collector.kind.value
                or (draft.tool, draft.capability) not in allowed
                or scope is None
                or draft.input_scope != scope
                or draft.normalized_scope != scope
            ):
                raise EvidenceToolError("ANALYSIS_FAILED")
        return drafts

    async def _persist_all(
        self, drafts: tuple[EvidenceDraft, ...]
    ) -> tuple[EvidenceReference, ...]:
        references: list[EvidenceReference] = []
        async with self._sessions() as session, session.begin():
            persistence = PersistEvidence(session)
            for draft in drafts:
                references.append(
                    await persistence.save(
                        self._request.rca_run_id,
                        self._specialist_run_id,
                        draft,
                    )
                )
        return tuple(references)

    def _record_from_committed(
        self, reference: EvidenceReference, draft: EvidenceDraft
    ) -> PersistedEvidence:
        return PersistedEvidence(
            reference=reference,
            rca_run_id=self._request.rca_run_id,
            specialist_run_id=self._specialist_run_id,
            evidence_type=draft.capability,
            source_endpoint=draft.endpoint_identity,
            tool_name=draft.tool,
            structured_data=draft.structured_json,
            metadata={
                "contentType": draft.content_type,
                "inputSha256": draft.input_sha256,
            },
        )

    def _build_receipt(
        self, persisted: tuple[PersistedEvidence, ...]
    ) -> EvidenceReceipt:
        all_chunks: list[tuple[EvidenceChunk, ...]] = []
        for record in persisted:
            self._ensure_record_owned(record)
            all_chunks.append(self._chunks(record))
        return EvidenceReceipt(
            specialist=self._collector.kind,
            references=tuple(record.reference for record in persisted),
            first_chunks=tuple(chunks[0] for chunks in all_chunks if chunks),
            total_chunks=sum(len(chunks) for chunks in all_chunks),
            truncated=any(
                chunk.truncated for chunks in all_chunks for chunk in chunks
            ),
        )

    def _empty_receipt(self) -> EvidenceReceipt:
        return EvidenceReceipt(
            specialist=self._collector.kind,
            references=(),
            first_chunks=(),
            total_chunks=0,
            truncated=False,
        )

    def _ensure_record_owned(self, record: PersistedEvidence) -> None:
        if (
            record.rca_run_id != self._request.rca_run_id
            or record.specialist_run_id != self._specialist_run_id
            or record.source_endpoint != self._collector.kind.value
            or not record.evidence_type.startswith(
                f"{self._collector.kind.value}."
            )
        ):
            raise EvidenceToolError("ANALYSIS_UNKNOWN_EVIDENCE")

    def _chunks(
        self, persisted: PersistedEvidence
    ) -> tuple[EvidenceChunk, ...]:
        return build_evidence_chunks(
            persisted.reference,
            persisted.structured_data,
            chunk_chars=self._chunk_chars,
            max_chunks=self._max_chunks,
            max_total_chars=self._max_total_chars,
        )


__all__ = [
    "EvidenceReceipt",
    "EvidenceToolError",
    "EvidenceToolSession",
]
