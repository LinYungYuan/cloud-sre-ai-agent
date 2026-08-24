from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

import sre_rca_worker.application.rca.processor as processor_module
from sre_rca_worker.agents.rca.models import (
    IncidentContext,
    InvestigationBundle,
    SpecialistFailure,
)
from sre_rca_worker.agents.specialists.base import SpecialistResult
from sre_rca_worker.application.rca.job_lifecycle import RcaJobClaim
from sre_rca_worker.application.rca.processor import ProductionRcaProcessor
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.domain.rca.models import RcaReportDraft
from sre_rca_worker.integrations.mcp.models import (
    CapabilitySet,
    CloudScope,
    SpecialistKind,
)


def test_total_transport_failure_requests_durable_job_retry() -> None:
    bundle = InvestigationBundle(
        failures=(
            SpecialistFailure(
                specialist=SpecialistKind.METRICS,
                code="SPECIALIST_TRANSPORT",
            ),
        )
    )

    with pytest.raises(ConnectionError, match="transient MCP failure"):
        ProductionRcaProcessor._raise_if_retryable_total_failure(bundle, 1)

    ProductionRcaProcessor._raise_if_retryable_total_failure(bundle, 3)


def test_partial_or_permanent_failure_does_not_request_durable_retry() -> None:
    permanent = InvestigationBundle(
        failures=(
            SpecialistFailure(
                specialist=SpecialistKind.METRICS,
                code="SPECIALIST_FAILED",
            ),
        )
    )
    ProductionRcaProcessor._raise_if_retryable_total_failure(permanent, 1)


@pytest.mark.asyncio
async def test_evidence_bearing_legacy_processor_calls_only_explicit_legacy_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)
    incident_id, rca_run_id, worker_job_id, evidence_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    reference = EvidenceReference(
        id=evidence_id,
        partition_timestamp=now,
    )
    summaries: tuple[dict[str, object], ...] = (
        {
            "specialist": "metrics",
            "summary": "persisted metrics summary",
            "confidence": 0.8,
            "evidenceReference": reference.model_dump(mode="json"),
        },
    )
    context = IncidentContext(
        incident_id=incident_id,
        rca_run_id=rca_run_id,
        alert_issue="CPU high",
        scope=CloudScope(provider="GCP", scope_id="project-a", safe=True),
        window_start=now - timedelta(minutes=15),
        window_end=now,
    )
    bundle = InvestigationBundle(
        results=(SpecialistResult(specialist=SpecialistKind.METRICS),),
    )
    legacy_report = RcaReportDraft(
        status="PARTIAL",
        summary_zh_tw="legacy synthesis",
        hypotheses=(),
        missing_evidence=("LEGACY_TEST",),
        remediation=("inspect",),
        verification_steps=("verify",),
    )
    calls = {"legacy": 0, "active": 0, "persisted": 0}

    class FakeSkills:
        @staticmethod
        def required_capabilities() -> dict[SpecialistKind, tuple[str, ...]]:
            return {}

        @staticmethod
        def get_for_agent(name: str) -> SimpleNamespace:
            assert name == "rca"
            return SimpleNamespace(body="exact RCA skill")

    class FakeWorkflow:
        def __init__(self, specialists: dict[SpecialistKind, object]) -> None:
            assert set(specialists) == set(SpecialistKind)

        async def run(
            self,
            actual_context: IncidentContext,
            capabilities: CapabilitySet,
            *,
            deadline: datetime,
        ) -> InvestigationBundle:
            assert actual_context == context
            assert capabilities == CapabilitySet(by_specialist={})
            assert deadline == now + timedelta(minutes=5)
            return bundle

    class FakeRootAdapter:
        def __init__(self, *, model_name: str, skill_instruction: str) -> None:
            assert model_name == "root-model"
            assert skill_instruction == "exact RCA skill"

        async def synthesize_legacy(
            self,
            *,
            alert_issue: str,
            evidence_summaries: tuple[dict[str, object], ...],
            known_evidence: tuple[EvidenceReference, ...],
            deadline: datetime,
        ) -> RcaReportDraft:
            calls["legacy"] += 1
            assert alert_issue == context.alert_issue
            assert evidence_summaries == summaries
            assert known_evidence == (reference,)
            assert deadline == now + timedelta(minutes=5)
            return legacy_report

        async def synthesize(self, **kwargs: object) -> RcaReportDraft:
            calls["active"] += 1
            raise AssertionError("legacy processor must not call ACTIVE synthesis")

    async def fake_discover(*args: object, **kwargs: object):
        return CapabilitySet(by_specialist={}), {}

    async def fake_load_context(
        self: ProductionRcaProcessor, claim: RcaJobClaim
    ) -> IncidentContext:
        assert claim.rca_run_id == rca_run_id
        return context

    async def fake_persist_failures(
        self: ProductionRcaProcessor,
        claim: RcaJobClaim,
        failures: tuple[SpecialistFailure, ...],
    ) -> None:
        assert failures == ()

    async def fake_persist_bundle(
        self: ProductionRcaProcessor,
        claim: RcaJobClaim,
        results: tuple[SpecialistResult, ...],
    ) -> tuple[
        tuple[EvidenceReference, ...],
        tuple[dict[str, object], ...],
    ]:
        assert results == bundle.results
        return (reference,), summaries

    async def fake_persist_report(
        self: ProductionRcaProcessor,
        claim: RcaJobClaim,
        report: RcaReportDraft,
    ) -> None:
        calls["persisted"] += 1
        assert report == legacy_report

    monkeypatch.setattr(processor_module, "McpClientFactory", lambda settings: object())
    monkeypatch.setattr(processor_module, "discover_capabilities", fake_discover)
    monkeypatch.setattr(processor_module, "RcaWorkflow", FakeWorkflow)
    monkeypatch.setattr(processor_module, "AdkRcaAgent", FakeRootAdapter)
    monkeypatch.setattr(ProductionRcaProcessor, "_load_context", fake_load_context)
    monkeypatch.setattr(
        ProductionRcaProcessor,
        "_persist_failures",
        fake_persist_failures,
    )
    monkeypatch.setattr(ProductionRcaProcessor, "_persist_bundle", fake_persist_bundle)
    monkeypatch.setattr(ProductionRcaProcessor, "_persist_report", fake_persist_report)

    processor = cast(Any, object.__new__(ProductionRcaProcessor))
    processor._settings = SimpleNamespace(
        mcp_capability_manifest=(),
        mcp_max_response_bytes=2 * 1024 * 1024,
        model_name="root-model",
    )
    processor._skills = FakeSkills()
    claim = RcaJobClaim(
        worker_job_id=worker_job_id,
        rca_run_id=rca_run_id,
        incident_id=incident_id,
        attempt_number=1,
        deadline_at=now + timedelta(minutes=5),
        lease_owner="test",
    )

    result = await processor(claim)

    assert result.status == "PARTIAL"
    assert calls == {"legacy": 1, "active": 0, "persisted": 1}
