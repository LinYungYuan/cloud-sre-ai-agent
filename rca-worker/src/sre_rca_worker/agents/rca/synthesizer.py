from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.domain.rca.models import RcaReportDraft


class RcaSynthesizer:
    """Validate structured RCA Agent output before durable persistence."""

    def validate(
        self,
        draft: RcaReportDraft,
        *,
        known_evidence: tuple[EvidenceReference, ...],
    ) -> RcaReportDraft:
        known = {(item.id, item.partition_timestamp) for item in known_evidence}
        cited = {
            (reference.id, reference.partition_timestamp)
            for claim in draft.claims
            for reference in claim.evidence
        }
        if not cited <= known:
            raise ValueError("RCA report cites unknown evidence")
        return draft

    def insufficient_evidence(self, *, provider: str | None) -> RcaReportDraft:
        provider_note = (
            "目前沒有 AWS MCP 證據。"
            if provider == "AWS"
            else "目前沒有可用的 MCP 證據。"
        )
        return RcaReportDraft(
            status="PARTIAL",
            summary_zh_tw=f"證據不足，無法確認根因；{provider_note}",
            claims=(),
            hypotheses=(),
            missing_evidence=("NO_SAFE_MCP_EVIDENCE",),
            remediation=("請由值班人員確認監控資料與資源範圍。",),
            verification_steps=("補齊證據後重新執行 RCA。",),
        )
