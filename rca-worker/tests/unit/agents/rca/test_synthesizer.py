from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sre_rca_worker.agents.rca.synthesizer import RcaSynthesizer
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.domain.rca.models import EvidenceClaim, RcaReportDraft


def _ref() -> EvidenceReference:
    return EvidenceReference(
        id=uuid4(), partition_timestamp=datetime(2026, 8, 13, tzinfo=UTC)
    )


def test_complete_report_requires_known_citations() -> None:
    known = _ref()
    draft = RcaReportDraft(
        status="COMPLETE",
        summary_zh_tw="CPU 使用率異常升高。",
        claims=(
            EvidenceClaim(
                statement="CPU 超過門檻", relation="SUPPORTS", evidence=(known,)
            ),
        ),
        hypotheses=("資料庫負載增加",),
        missing_evidence=(),
        remediation=("由值班人員檢查慢查詢",),
        verification_steps=("確認 CPU 回復正常",),
    )
    assert (
        RcaSynthesizer().validate(draft, known_evidence=(known,)).status == "COMPLETE"
    )
    with pytest.raises(ValueError, match="unknown evidence"):
        RcaSynthesizer().validate(draft, known_evidence=(_ref(),))


def test_no_mcp_scope_produces_honest_partial_report() -> None:
    report = RcaSynthesizer().insufficient_evidence(provider="AWS")
    assert report.status == "PARTIAL"
    assert "證據不足" in report.summary_zh_tw
    assert "AWS MCP" in report.summary_zh_tw
    assert report.claims == ()
