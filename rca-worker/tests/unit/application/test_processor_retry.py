import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

import sre_rca_worker.application.rca.processor as processor_module
from sre_rca_worker.agents.rca.models import (
    IncidentContext,
    InvestigationBundle,
    SpecialistAnalysisBundle,
    SpecialistAnalysisResult,
    SpecialistFailure,
)
from sre_rca_worker.agents.specialists.base import SpecialistRequest, SpecialistResult
from sre_rca_worker.application.rca.job_lifecycle import RcaJobClaim, RcaJobHandler
from sre_rca_worker.application.rca.processor import ProductionRcaProcessor
from sre_rca_worker.config.settings import (
    SpecialistAnalysisMode,
    WorkerSettings,
)
from sre_rca_worker.domain.evidence.analysis import (
    SpecialistAnalysisDraft,
    SpecialistObservation,
)
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.domain.rca.models import (
    EvidenceClaim,
    RcaHypothesis,
    RcaReportDraft,
)
from sre_rca_worker.integrations.mcp.models import (
    AllowedTool,
    CapabilitySet,
    CloudScope,
    SpecialistKind,
)

ORDER = (SpecialistKind.METRICS, SpecialistKind.TRACE, SpecialistKind.LOG)


def _allowed_tool(kind: SpecialistKind) -> AllowedTool:
    return AllowedTool(
        name=f"{kind.value}_query",
        capability=f"{kind.value}.query",
        endpoint_identity=kind.value,
        input_schema={"type": "object"},
    )


def _analysis_result(
    kind: SpecialistKind,
    *,
    index: int,
    status: str = "COMPLETE",
) -> SpecialistAnalysisResult:
    reference = EvidenceReference(
        id=UUID(f"00000000-0000-0000-0000-{index:012d}"),
        partition_timestamp=datetime(2026, 8, 25, 8, index, tzinfo=UTC),
    )
    return SpecialistAnalysisResult(
        analysis=SpecialistAnalysisDraft(
            specialist=kind,
            status=cast(Any, status),
            observations=(
                SpecialistObservation(
                    statement=f"{kind.value} concrete observation",
                    confidence=0.9,
                    relation="SUPPORTS",
                    evidence=(reference,),
                ),
            ),
            missing_evidence=(
                ("ANALYSIS_INPUT_TRUNCATED",) if status == "PARTIAL" else ()
            ),
        ),
        known_evidence=(reference,),
    )


def _complete_report(reference: EvidenceReference) -> RcaReportDraft:
    return RcaReportDraft(
        status="COMPLETE",
        summary_zh_tw="root complete",
        hypotheses=(
            RcaHypothesis(
                statement="database load",
                confidence=0.9,
                claims=(
                    EvidenceClaim(
                        statement="latency follows load",
                        relation="SUPPORTS",
                        evidence=(reference,),
                    ),
                ),
            ),
        ),
        missing_evidence=(),
        remediation=("limit load",),
        verification_steps=("verify latency",),
    )


def _processor_settings(mode: SpecialistAnalysisMode) -> SimpleNamespace:
    return SimpleNamespace(
        specialist_analysis_mode=mode,
        mcp_capability_manifest=(),
        mcp_max_response_bytes=2 * 1024 * 1024,
        evidence_chunk_chars=8_000,
        evidence_max_chunks=4,
        evidence_max_total_chars=32_000,
        specialist_max_tool_calls=5,
        model_name="root-and-specialist-model",
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


def test_job_claim_deadline_matches_the_300_second_configured_hard_cap() -> None:
    claim_sql = inspect.getsource(RcaJobHandler._claim)

    assert WorkerSettings.model_fields["rca_deadline_seconds"].default == 300
    assert claim_sql.count("job.created_at + interval '5 minutes'") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_calls"),
    [
        (
            SpecialistAnalysisMode.DISABLED,
            {
                "legacy_workflow": 1,
                "analysis_workflow": 0,
                "legacy_root": 1,
                "active_root": 0,
            },
        ),
        (
            SpecialistAnalysisMode.SHADOW,
            {
                "legacy_workflow": 0,
                "analysis_workflow": 1,
                "legacy_root": 1,
                "active_root": 0,
            },
        ),
        (
            SpecialistAnalysisMode.ACTIVE,
            {
                "legacy_workflow": 0,
                "analysis_workflow": 1,
                "legacy_root": 0,
                "active_root": 1,
            },
        ),
    ],
)
async def test_processor_rollout_modes_keep_root_inputs_strictly_separate(
    monkeypatch: pytest.MonkeyPatch,
    mode: SpecialistAnalysisMode,
    expected_calls: dict[str, int],
) -> None:
    now = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)
    context = IncidentContext(
        incident_id=uuid4(),
        rca_run_id=uuid4(),
        alert_issue="untrusted raw alert",
        scope=CloudScope(provider="GCP", scope_id="project-a", safe=True),
        window_start=now - timedelta(minutes=15),
        window_end=now,
    )
    claim = RcaJobClaim(
        worker_job_id=uuid4(),
        rca_run_id=context.rca_run_id,
        incident_id=context.incident_id,
        attempt_number=1,
        deadline_at=now + timedelta(minutes=5),
        lease_owner="test",
    )
    results = tuple(
        _analysis_result(kind, index=index) for index, kind in enumerate(ORDER, start=1)
    )
    analysis_bundle = SpecialistAnalysisBundle(results=results)
    legacy_reference = results[0].known_evidence[0]
    legacy_summaries: tuple[dict[str, object], ...] = (
        {
            "specialist": "metrics",
            "summary": "legacy persisted evidence summary",
            "confidence": 0.5,
            "evidenceReference": legacy_reference.model_dump(mode="json"),
        },
    )
    capabilities = CapabilitySet(
        by_specialist={kind: (_allowed_tool(kind),) for kind in ORDER}
    )
    calls = {
        "legacy_workflow": 0,
        "analysis_workflow": 0,
        "legacy_root": 0,
        "active_root": 0,
    }
    root_inputs: dict[str, object] = {}
    persisted_reports: list[RcaReportDraft] = []

    class FakeLegacyWorkflow:
        def __init__(self, specialists: object) -> None:
            assert specialists

        async def run(self, *args: object, **kwargs: object) -> InvestigationBundle:
            calls["legacy_workflow"] += 1
            return InvestigationBundle(
                results=(SpecialistResult(specialist=SpecialistKind.METRICS),)
            )

    class FakeAnalysisWorkflow:
        def __init__(self, invoker: object) -> None:
            assert invoker

        async def run(
            self, *args: object, **kwargs: object
        ) -> SpecialistAnalysisBundle:
            calls["analysis_workflow"] += 1
            return analysis_bundle

    class FakeRootAgent:
        async def synthesize_legacy(self, **kwargs: object) -> RcaReportDraft:
            calls["legacy_root"] += 1
            root_inputs["legacy"] = kwargs
            assert "specialist_analyses" not in kwargs
            return _complete_report(
                cast(tuple[EvidenceReference, ...], kwargs["known_evidence"])[0]
            )

        async def synthesize(self, **kwargs: object) -> RcaReportDraft:
            calls["active_root"] += 1
            root_inputs["active"] = kwargs
            assert "evidence_summaries" not in kwargs
            analyses = cast(
                tuple[SpecialistAnalysisDraft, ...], kwargs["specialist_analyses"]
            )
            assert tuple(item.specialist for item in analyses) == ORDER
            assert (
                "raw"
                not in str(
                    tuple(item.model_dump(mode="json") for item in analyses)
                ).lower()
            )
            return _complete_report(
                cast(tuple[EvidenceReference, ...], kwargs["known_evidence"])[0]
            )

    def fake_root_factory(**kwargs: object) -> FakeRootAgent:
        assert kwargs == {
            "model_name": "root-and-specialist-model",
            "skill_instruction": cast(Any, processor)._skills.get_for_agent("rca").body,
        }
        return FakeRootAgent()

    async def fake_discover(*args: object, **kwargs: object):
        return capabilities, {kind: object() for kind in ORDER}

    async def fake_load_context(
        self: ProductionRcaProcessor, actual_claim: RcaJobClaim
    ) -> IncidentContext:
        assert actual_claim == claim
        return context

    async def fake_persist_bundle(
        self: ProductionRcaProcessor, actual_claim: RcaJobClaim, items: object
    ) -> tuple[tuple[EvidenceReference, ...], tuple[dict[str, object], ...]]:
        return (legacy_reference,), legacy_summaries

    async def fake_persist_nothing(self: ProductionRcaProcessor, *args: object) -> None:
        return None

    async def fake_persist_analyses(
        self: ProductionRcaProcessor,
        actual_claim: RcaJobClaim,
        items: tuple[SpecialistAnalysisResult, ...],
    ) -> tuple[tuple[SpecialistAnalysisResult, ...], tuple[SpecialistFailure, ...]]:
        return items, ()

    async def fake_persist_report(
        self: ProductionRcaProcessor,
        actual_claim: RcaJobClaim,
        report: RcaReportDraft,
    ) -> None:
        persisted_reports.append(report)

    monkeypatch.setattr(processor_module, "McpClientFactory", lambda settings: object())
    monkeypatch.setattr(processor_module, "discover_capabilities", fake_discover)
    monkeypatch.setattr(processor_module, "RcaWorkflow", FakeLegacyWorkflow)
    monkeypatch.setattr(
        processor_module,
        "SpecialistAnalysisWorkflow",
        FakeAnalysisWorkflow,
        raising=False,
    )
    monkeypatch.setattr(ProductionRcaProcessor, "_load_context", fake_load_context)
    monkeypatch.setattr(ProductionRcaProcessor, "_persist_bundle", fake_persist_bundle)
    monkeypatch.setattr(
        ProductionRcaProcessor, "_persist_failures", fake_persist_nothing
    )
    monkeypatch.setattr(
        ProductionRcaProcessor,
        "_persist_specialist_analyses",
        fake_persist_analyses,
        raising=False,
    )
    monkeypatch.setattr(
        ProductionRcaProcessor,
        "_persist_analysis_failures",
        fake_persist_nothing,
        raising=False,
    )
    monkeypatch.setattr(ProductionRcaProcessor, "_persist_report", fake_persist_report)

    processor = ProductionRcaProcessor(
        cast(Any, object()),
        cast(Any, _processor_settings(mode)),
        root_agent_factory=fake_root_factory,
    )
    result = await processor(claim)

    assert calls == expected_calls
    assert result.status == "COMPLETE"
    assert len(persisted_reports) == 1
    if mode is SpecialistAnalysisMode.SHADOW:
        legacy = cast(dict[str, object], root_inputs["legacy"])
        assert legacy["evidence_summaries"] != tuple(
            item.analysis.model_dump(mode="json") for item in results
        )
    if mode is SpecialistAnalysisMode.ACTIVE:
        active = cast(dict[str, object], root_inputs["active"])
        assert active["specialist_analyses"] == tuple(item.analysis for item in results)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    (
        None,
        CloudScope(provider="AWS", scope_id="123456789012", safe=True),
        CloudScope(provider="GCP", scope_id="project-a", safe=False),
    ),
    ids=("no-scope", "aws", "unsafe-gcp"),
)
async def test_no_safe_route_avoids_factory_discovery_and_all_models(
    monkeypatch: pytest.MonkeyPatch,
    scope: CloudScope | None,
) -> None:
    now = datetime(2026, 8, 25, 8, 30, tzinfo=UTC)
    context = IncidentContext(
        incident_id=uuid4(),
        rca_run_id=uuid4(),
        alert_issue="untrusted alert",
        scope=scope,
        window_start=now - timedelta(minutes=15),
        window_end=now,
    )
    claim = RcaJobClaim(
        worker_job_id=uuid4(),
        rca_run_id=context.rca_run_id,
        incident_id=context.incident_id,
        attempt_number=1,
        deadline_at=now + timedelta(minutes=5),
        lease_owner="test",
    )
    persisted_reports: list[RcaReportDraft] = []

    def fail_factory(*args: object, **kwargs: object) -> object:
        raise AssertionError("no-safe route must not construct MCP clients")

    async def fail_discovery(*args: object, **kwargs: object) -> object:
        raise AssertionError("no-safe route must not discover MCP capabilities")

    async def fake_load_context(
        self: ProductionRcaProcessor, actual_claim: RcaJobClaim
    ) -> IncidentContext:
        assert actual_claim == claim
        return context

    async def fake_persist_report(
        self: ProductionRcaProcessor,
        actual_claim: RcaJobClaim,
        report: RcaReportDraft,
    ) -> None:
        assert actual_claim == claim
        persisted_reports.append(report)

    monkeypatch.setattr(processor_module, "McpClientFactory", fail_factory)
    monkeypatch.setattr(processor_module, "discover_capabilities", fail_discovery)
    monkeypatch.setattr(ProductionRcaProcessor, "_load_context", fake_load_context)
    monkeypatch.setattr(ProductionRcaProcessor, "_persist_report", fake_persist_report)

    processor = ProductionRcaProcessor(
        cast(Any, object()),
        cast(Any, _processor_settings(SpecialistAnalysisMode.ACTIVE)),
        root_agent_factory=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("no-safe route must not construct a Root model")
        ),
        specialist_agent_factory=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("no-safe route must not construct a Specialist model")
        ),
    )

    result = await processor(claim)

    assert result.status == "PARTIAL"
    assert len(persisted_reports) == 1
    assert persisted_reports[0].hypotheses == ()


async def _exercise_analysis_outcome(
    *,
    bundle: SpecialistAnalysisBundle,
    attempt_number: int = 1,
    root_report: RcaReportDraft | None = None,
    scope: CloudScope | None = None,
    analysis_persistence_outcome: tuple[
        tuple[SpecialistAnalysisResult, ...],
        tuple[SpecialistFailure, ...],
    ]
    | None = None,
) -> tuple[RcaReportDraft | None, dict[str, object]]:
    now = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    context = IncidentContext(
        incident_id=uuid4(),
        rca_run_id=uuid4(),
        alert_issue="untrusted alert",
        scope=scope or CloudScope(provider="GCP", scope_id="project-a", safe=True),
        window_start=now - timedelta(minutes=15),
        window_end=now,
    )
    claim = RcaJobClaim(
        worker_job_id=uuid4(),
        rca_run_id=context.rca_run_id,
        incident_id=context.incident_id,
        attempt_number=attempt_number,
        deadline_at=now + timedelta(minutes=5),
        lease_owner="test",
    )
    calls: dict[str, object] = {
        "root": 0,
        "persisted_results": (),
        "persisted_failures": (),
        "root_analyses": (),
    }

    class FakeWorkflow:
        async def run(
            self, *args: object, **kwargs: object
        ) -> SpecialistAnalysisBundle:
            return bundle

    class FakeRoot:
        async def synthesize(self, **kwargs: object) -> RcaReportDraft:
            calls["root"] = cast(int, calls["root"]) + 1
            calls["root_analyses"] = kwargs["specialist_analyses"]
            if root_report is not None:
                return root_report
            known = cast(tuple[EvidenceReference, ...], kwargs["known_evidence"])
            return _complete_report(known[0])

        async def synthesize_legacy(self, **kwargs: object) -> RcaReportDraft:
            raise AssertionError("ACTIVE outcome must not use legacy Root")

    processor = ProductionRcaProcessor(
        cast(Any, object()),
        cast(Any, _processor_settings(SpecialistAnalysisMode.ACTIVE)),
        root_agent_factory=lambda **kwargs: FakeRoot(),
    )
    cast(Any, processor)._analysis_workflow_factory = lambda invoker: FakeWorkflow()

    async def persist_results(
        actual_claim: RcaJobClaim,
        results: tuple[SpecialistAnalysisResult, ...],
    ) -> tuple[tuple[SpecialistAnalysisResult, ...], tuple[SpecialistFailure, ...]]:
        calls["persisted_results"] = results
        return analysis_persistence_outcome or (results, ())

    async def persist_failures(
        actual_claim: RcaJobClaim,
        failures: tuple[SpecialistFailure, ...],
    ) -> None:
        calls["persisted_failures"] = failures

    cast(Any, processor)._persist_specialist_analyses = persist_results
    cast(Any, processor)._persist_analysis_failures = persist_failures
    capabilities = CapabilitySet(
        by_specialist={kind: (_allowed_tool(kind),) for kind in ORDER}
    )
    try:
        report = await processor._run_specialist_analysis(
            claim,
            context,
            capabilities,
            cast(Any, {kind: object() for kind in ORDER}),
            mode=SpecialistAnalysisMode.ACTIVE,
        )
    except ConnectionError:
        calls["raised_transport"] = True
        report = None
    return report, calls


@pytest.mark.asyncio
async def test_some_observations_plus_failure_preserves_analysis_and_is_partial() -> (
    None
):
    metrics = _analysis_result(SpecialistKind.METRICS, index=1)
    report, calls = await _exercise_analysis_outcome(
        bundle=SpecialistAnalysisBundle(
            results=(metrics,),
            failures=(
                SpecialistFailure(
                    specialist=SpecialistKind.TRACE,
                    code="ANALYSIS_SCHEMA_INVALID",
                ),
            ),
        )
    )

    assert report is not None
    assert report.status == "PARTIAL"
    assert report.hypotheses
    assert calls["persisted_results"] == (metrics,)
    assert cast(tuple[SpecialistFailure, ...], calls["persisted_failures"])[0].code == (
        "ANALYSIS_SCHEMA_INVALID"
    )
    assert calls["root"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("codes", "attempt_number", "expected_retry"),
    [
        (("MCP_TRANSPORT", "MCP_TRANSPORT"), 1, True),
        (("MCP_TRANSPORT", "MCP_TRANSPORT"), 3, False),
        (("MCP_TRANSPORT", "ANALYSIS_FAILED"), 1, False),
        (("ANALYSIS_FAILED", "ANALYSIS_SCHEMA_INVALID"), 1, False),
    ],
)
async def test_all_failed_analysis_matrix_controls_durable_retry(
    codes: tuple[str, str],
    attempt_number: int,
    expected_retry: bool,
) -> None:
    bundle = SpecialistAnalysisBundle(
        failures=tuple(
            SpecialistFailure(specialist=kind, code=code)
            for kind, code in zip(ORDER, codes, strict=False)
        )
    )
    report, calls = await _exercise_analysis_outcome(
        bundle=bundle,
        attempt_number=attempt_number,
    )

    assert calls["persisted_failures"] == bundle.failures
    assert calls.get("raised_transport", False) is expected_retry
    assert calls["root"] == 0
    if expected_retry:
        assert report is None
    else:
        assert report is not None
        assert report.status == "FAILED"
        assert report.hypotheses == ()


@pytest.mark.asyncio
async def test_fixed_analysis_order_and_root_partial_decision_are_preserved() -> None:
    results = tuple(
        _analysis_result(kind, index=index) for index, kind in enumerate(ORDER, start=1)
    )
    root_partial = RcaReportDraft(
        status="PARTIAL",
        summary_zh_tw="root needs more evidence",
        hypotheses=(),
        missing_evidence=("ROOT_UNCERTAIN",),
        remediation=("inspect",),
        verification_steps=("verify",),
    )
    report, calls = await _exercise_analysis_outcome(
        bundle=SpecialistAnalysisBundle(results=tuple(reversed(results))),
        root_report=root_partial,
    )

    assert report == root_partial
    analyses = cast(tuple[SpecialistAnalysisDraft, ...], calls["root_analyses"])
    assert tuple(item.specialist for item in analyses) == ORDER


@pytest.mark.asyncio
async def test_no_safe_route_returns_honest_partial_without_root_model() -> None:
    report, calls = await _exercise_analysis_outcome(
        bundle=SpecialistAnalysisBundle(),
        scope=CloudScope(provider="AWS", scope_id="123456789012", safe=True),
    )

    assert report is not None
    assert report.status == "PARTIAL"
    assert report.hypotheses == ()
    assert "AWS MCP" in report.summary_zh_tw
    assert calls["root"] == 0


@pytest.mark.asyncio
async def test_audit_ownership_failure_is_permanent_and_never_retried_as_transport() -> (
    None
):
    metrics = _analysis_result(SpecialistKind.METRICS, index=1)
    audit_failure = SpecialistFailure(
        specialist=SpecialistKind.METRICS,
        code="ANALYSIS_UNKNOWN_EVIDENCE",
    )

    report, calls = await _exercise_analysis_outcome(
        bundle=SpecialistAnalysisBundle(results=(metrics,)),
        analysis_persistence_outcome=((), (audit_failure,)),
    )

    assert report is not None
    assert report.status == "FAILED"
    assert report.hypotheses == ()
    assert calls["root"] == 0
    assert calls.get("raised_transport", False) is False
    assert calls["persisted_failures"] == (audit_failure,)


@pytest.mark.asyncio
async def test_each_transport_attempt_builds_fresh_session_and_reuses_committed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    run_id = uuid4()
    specialist_run_id = uuid4()
    reference = EvidenceReference(id=uuid4(), partition_timestamp=now)
    request = SpecialistRequest(
        incident_id=uuid4(),
        rca_run_id=run_id,
        alert_issue="CPU high",
        scope=CloudScope(provider="GCP", scope_id="project-a", safe=True),
        window_start=now - timedelta(minutes=15),
        window_end=now,
        available_tools=(_allowed_tool(SpecialistKind.METRICS),),
    )
    sessions: list[object] = []
    agents: list[object] = []
    row_requests: list[tuple[UUID, SpecialistKind]] = []
    state: dict[str, object] = {"persisted": False, "mcp_calls": 0}

    class FakeEvidenceSession:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["specialist_run_id"] == specialist_run_id
            self.known_evidence: tuple[EvidenceReference, ...] = (
                (reference,) if state["persisted"] else ()
            )
            sessions.append(self)

        async def collect_evidence(self) -> None:
            if not state["persisted"]:
                state["mcp_calls"] = cast(int, state["mcp_calls"]) + 1
                state["persisted"] = True
            self.known_evidence = (reference,)

    class FakeSpecialistAgent:
        async def analyze(self, **kwargs: object) -> SpecialistAnalysisDraft:
            tools = cast(FakeEvidenceSession, kwargs["evidence_tools"])
            await tools.collect_evidence()
            return SpecialistAnalysisDraft(
                specialist=SpecialistKind.METRICS,
                status="COMPLETE",
                observations=(
                    SpecialistObservation(
                        statement="persisted CPU observation",
                        confidence=0.9,
                        relation="SUPPORTS",
                        evidence=(reference,),
                    ),
                ),
            )

    def fake_agent_factory(**kwargs: object) -> FakeSpecialistAgent:
        assert kwargs["kind"] is SpecialistKind.METRICS
        assert kwargs["skill_instruction"]
        agent = FakeSpecialistAgent()
        agents.append(agent)
        return agent

    processor = ProductionRcaProcessor(
        cast(Any, object()),
        cast(Any, _processor_settings(SpecialistAnalysisMode.ACTIVE)),
        specialist_agent_factory=fake_agent_factory,
    )

    async def get_or_create(actual_run: UUID, kind: SpecialistKind) -> UUID:
        row_requests.append((actual_run, kind))
        return specialist_run_id

    cast(Any, processor)._get_or_create_specialist_run = get_or_create
    monkeypatch.setattr(processor_module, "EvidenceToolSession", FakeEvidenceSession)

    first = await processor._invoke_specialist_branch(
        request,
        SpecialistKind.METRICS,
        now + timedelta(minutes=5),
        clients=cast(Any, {SpecialistKind.METRICS: object()}),
    )
    second = await processor._invoke_specialist_branch(
        request,
        SpecialistKind.METRICS,
        now + timedelta(minutes=5),
        clients=cast(Any, {SpecialistKind.METRICS: object()}),
    )

    assert first == second
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
    assert len(agents) == 2
    assert agents[0] is not agents[1]
    assert row_requests == [
        (run_id, SpecialistKind.METRICS),
        (run_id, SpecialistKind.METRICS),
    ]
    assert state["mcp_calls"] == 1


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
