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
            for hypothesis in draft.hypotheses
            for claim in hypothesis.claims
            for reference in claim.evidence
        }
        if not cited <= known:
            raise ValueError("RCA report cites unknown evidence")
        return draft

    def with_specialist_failures(self, draft: RcaReportDraft) -> RcaReportDraft:
        if draft.status != "COMPLETE":
            return draft
        return draft.model_copy(
            update={
                "status": "PARTIAL",
                "missing_evidence": (*draft.missing_evidence, "SPECIALIST_FAILURE"),
            }
        )

    def insufficient_evidence(self, *, provider: str | None) -> RcaReportDraft:
        provider_note = (
            "目前沒有 AWS MCP 證據。"
            if provider == "AWS"
            else "目前沒有可用的 MCP 證據。"
        )
        return RcaReportDraft(
            status="PARTIAL",
            summary_zh_tw=f"證據不足，無法確認根因；{provider_note}",
            hypotheses=(),
            missing_evidence=("NO_SAFE_MCP_EVIDENCE",),
            remediation=("請由值班人員確認監控資料與資源範圍。",),
            verification_steps=("補齊證據後重新執行 RCA。",),
        )

    def failed_analysis(self) -> RcaReportDraft:
        return RcaReportDraft(
            status="FAILED",
            summary_zh_tw="Specialist 分析失敗，沒有可用證據，無法確認根因。",
            hypotheses=(),
            missing_evidence=("NO_USABLE_SPECIALIST_EVIDENCE",),
            remediation=("請確認 MCP 服務狀態後重新執行 RCA。",),
            verification_steps=("重新執行後確認至少取得一項可追溯證據。",),
        )
