from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from sre_rca_worker.domain.evidence.models import EvidenceDraft, EvidenceReference
from sre_rca_worker.persistence.repositories.rca import (
    PersistedEvidence,
    RcaRepository,
)


class PersistEvidence:
    def __init__(self, session: AsyncSession) -> None:
        self._repository = RcaRepository(session)

    async def save(
        self,
        rca_run_id: UUID,
        specialist_run_id: UUID | None,
        draft: EvidenceDraft,
    ) -> EvidenceReference:
        return await self._repository.insert_evidence(
            rca_run_id, specialist_run_id, draft
        )

    async def list_specialist_evidence(
        self, rca_run_id: UUID, specialist_run_id: UUID
    ) -> tuple[PersistedEvidence, ...]:
        return await self._repository.list_specialist_evidence(
            rca_run_id, specialist_run_id
        )

    async def get_specialist_evidence(
        self,
        rca_run_id: UUID,
        specialist_run_id: UUID,
        evidence_id: UUID,
    ) -> PersistedEvidence | None:
        return await self._repository.get_specialist_evidence(
            rca_run_id, specialist_run_id, evidence_id
        )
