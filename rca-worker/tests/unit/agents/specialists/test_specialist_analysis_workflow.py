from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from sre_rca_worker.agents.rca.models import (
    IncidentContext,
    SpecialistAnalysisBundle,
    SpecialistAnalysisResult,
    SpecialistFailure,
)
from sre_rca_worker.agents.specialists.base import SpecialistRequest
from sre_rca_worker.agents.specialists.validator import (
    SpecialistAnalysisValidationError,
)
from sre_rca_worker.agents.specialists.workflow import SpecialistAnalysisWorkflow
from sre_rca_worker.application.rca.evidence_tools import EvidenceToolError
from sre_rca_worker.domain.evidence.analysis import (
    SpecialistAnalysisDraft,
    SpecialistObservation,
    StableSpecialistCode,
)
from sre_rca_worker.domain.evidence.chunking import McpPayloadTooLargeError
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.integrations.mcp.models import (
    AllowedTool,
    CapabilitySet,
    CloudScope,
    SpecialistKind,
)

NOW = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
ORDER = (SpecialistKind.METRICS, SpecialistKind.TRACE, SpecialistKind.LOG)
REFERENCES = {
    kind: EvidenceReference(
        id=UUID(f"00000000-0000-0000-0000-{index:012d}"),
        partition_timestamp=NOW,
    )
    for index, kind in enumerate(ORDER, start=1)
}


def _tool(kind: SpecialistKind) -> AllowedTool:
    return AllowedTool(
        name=f"{kind.value}_query",
        capability=f"{kind.value}.query",
        endpoint_identity=kind.value,
        input_schema={"type": "object"},
    )


def _capabilities(*kinds: SpecialistKind) -> CapabilitySet:
    return CapabilitySet(by_specialist={kind: (_tool(kind),) for kind in kinds})


def _context(*, scope: CloudScope | None = None) -> IncidentContext:
    return IncidentContext(
        incident_id=UUID("10000000-0000-0000-0000-000000000001"),
        rca_run_id=UUID("20000000-0000-0000-0000-000000000001"),
        alert_issue="untrusted alert text",
        scope=(
            CloudScope(provider="GCP", scope_id="project-a", safe=True)
            if scope is None
            else scope
        ),
        window_start=NOW - timedelta(minutes=15),
        window_end=NOW,
    )


def _context_without_scope() -> IncidentContext:
    return IncidentContext(
        incident_id=UUID("10000000-0000-0000-0000-000000000001"),
        rca_run_id=UUID("20000000-0000-0000-0000-000000000001"),
        alert_issue="untrusted alert text",
        scope=None,
        window_start=NOW - timedelta(minutes=15),
        window_end=NOW,
    )


def _result(kind: SpecialistKind) -> SpecialistAnalysisResult:
    reference = REFERENCES[kind]
    return SpecialistAnalysisResult(
        analysis=SpecialistAnalysisDraft(
            specialist=kind,
            status="COMPLETE",
            observations=(
                SpecialistObservation(
                    statement=f"{kind.value} observation",
                    confidence=0.9,
                    relation="SUPPORTS",
                    evidence=(reference,),
                ),
            ),
        ),
        known_evidence=(reference,),
    )


BranchInvoker = Callable[
    [SpecialistRequest, SpecialistKind, datetime],
    Awaitable[SpecialistAnalysisResult],
]


@pytest.mark.asyncio
async def test_selected_branches_start_concurrently_and_return_in_fixed_order() -> None:
    started: list[SpecialistKind] = []
    completed: list[SpecialistKind] = []
    all_started = asyncio.Event()
    release = {kind: asyncio.Event() for kind in ORDER}
    finished = {kind: asyncio.Event() for kind in ORDER}

    async def invoke(
        request: SpecialistRequest, kind: SpecialistKind, deadline: datetime
    ) -> SpecialistAnalysisResult:
        assert request.available_tools == (_tool(kind),)
        assert deadline == NOW + timedelta(minutes=1)
        started.append(kind)
        if len(started) == len(ORDER):
            all_started.set()
        await release[kind].wait()
        completed.append(kind)
        finished[kind].set()
        return _result(kind)

    task = asyncio.create_task(
        SpecialistAnalysisWorkflow(cast(BranchInvoker, invoke), clock=lambda: NOW).run(
            _context(),
            _capabilities(*ORDER),
            deadline=NOW + timedelta(minutes=1),
        )
    )
    await asyncio.wait_for(all_started.wait(), timeout=1)
    assert set(started) == set(ORDER)

    for kind in (SpecialistKind.LOG, SpecialistKind.METRICS, SpecialistKind.TRACE):
        release[kind].set()
        await asyncio.wait_for(finished[kind].wait(), timeout=1)
    bundle = await task

    assert completed == [
        SpecialistKind.LOG,
        SpecialistKind.METRICS,
        SpecialistKind.TRACE,
    ]
    assert tuple(result.analysis.specialist for result in bundle.results) == ORDER
    assert tuple(result.known_evidence for result in bundle.results) == tuple(
        (REFERENCES[kind],) for kind in ORDER
    )
    assert bundle.failures == ()


@pytest.mark.asyncio
async def test_missing_capability_skips_only_that_specialist() -> None:
    calls: list[tuple[SpecialistKind, SpecialistRequest]] = []

    async def invoke(
        request: SpecialistRequest, kind: SpecialistKind, deadline: datetime
    ) -> SpecialistAnalysisResult:
        calls.append((kind, request))
        return _result(kind)

    bundle = await SpecialistAnalysisWorkflow(
        cast(BranchInvoker, invoke), clock=lambda: NOW
    ).run(
        _context(),
        _capabilities(SpecialistKind.METRICS, SpecialistKind.LOG),
        deadline=NOW + timedelta(minutes=1),
    )

    assert [kind for kind, _ in calls] == [
        SpecialistKind.METRICS,
        SpecialistKind.LOG,
    ]
    assert [request.available_tools for _, request in calls] == [
        (_tool(SpecialistKind.METRICS),),
        (_tool(SpecialistKind.LOG),),
    ]
    assert tuple(result.analysis.specialist for result in bundle.results) == (
        SpecialistKind.METRICS,
        SpecialistKind.LOG,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        _context(scope=CloudScope(provider="GCP", scope_id="project-a", safe=False)),
        _context(scope=CloudScope(provider="AWS", scope_id="123456789012", safe=True)),
        _context_without_scope(),
    ],
    ids=["unsafe-gcp", "aws", "no-scope"],
)
async def test_unsafe_or_unsupported_scope_never_creates_a_branch(
    context: IncidentContext,
) -> None:
    calls: list[SpecialistKind] = []

    async def invoke(
        request: SpecialistRequest, kind: SpecialistKind, deadline: datetime
    ) -> SpecialistAnalysisResult:
        calls.append(kind)
        raise AssertionError("unsafe routes must not create a branch")

    bundle = await SpecialistAnalysisWorkflow(
        cast(BranchInvoker, invoke), clock=lambda: NOW
    ).run(
        context,
        _capabilities(*ORDER),
        deadline=NOW + timedelta(minutes=1),
    )

    assert bundle == SpecialistAnalysisBundle()
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (TimeoutError("secret timeout detail"), "ANALYSIS_TIMEOUT"),
        (
            SpecialistAnalysisValidationError("ANALYSIS_SCHEMA_INVALID"),
            "ANALYSIS_SCHEMA_INVALID",
        ),
        (
            McpPayloadTooLargeError("secret payload detail"),
            "MCP_PAYLOAD_TOO_LARGE",
        ),
        (
            EvidenceToolError("ANALYSIS_UNKNOWN_EVIDENCE"),
            "ANALYSIS_UNKNOWN_EVIDENCE",
        ),
    ],
    ids=["timeout", "schema", "payload", "ownership"],
)
async def test_one_permanent_failure_preserves_other_results_without_secret_text(
    failure: Exception, expected_code: str
) -> None:
    calls: list[SpecialistKind] = []

    async def invoke(
        request: SpecialistRequest, kind: SpecialistKind, deadline: datetime
    ) -> SpecialistAnalysisResult:
        calls.append(kind)
        if kind is SpecialistKind.TRACE:
            raise failure
        return _result(kind)

    bundle = await SpecialistAnalysisWorkflow(
        cast(BranchInvoker, invoke), clock=lambda: NOW
    ).run(
        _context(),
        _capabilities(*ORDER),
        deadline=NOW + timedelta(minutes=1),
    )

    assert tuple(result.analysis.specialist for result in bundle.results) == (
        SpecialistKind.METRICS,
        SpecialistKind.LOG,
    )
    assert [(item.specialist, item.code) for item in bundle.failures] == [
        (SpecialistKind.TRACE, expected_code)
    ]
    assert calls.count(SpecialistKind.TRACE) == 1
    assert "secret" not in repr(bundle).lower()


@pytest.mark.asyncio
async def test_all_permanent_failures_return_only_fixed_order_failures() -> None:
    failures: dict[SpecialistKind, Exception] = {
        SpecialistKind.METRICS: RuntimeError("secret generic detail"),
        SpecialistKind.TRACE: SpecialistAnalysisValidationError(
            "ANALYSIS_SCHEMA_INVALID"
        ),
        SpecialistKind.LOG: McpPayloadTooLargeError("secret payload detail"),
    }

    async def invoke(
        request: SpecialistRequest, kind: SpecialistKind, deadline: datetime
    ) -> SpecialistAnalysisResult:
        await asyncio.sleep(
            {
                SpecialistKind.METRICS: 0.003,
                SpecialistKind.TRACE: 0.002,
                SpecialistKind.LOG: 0.001,
            }[kind]
        )
        raise failures[kind]

    bundle = await SpecialistAnalysisWorkflow(
        cast(BranchInvoker, invoke), clock=lambda: NOW
    ).run(
        _context(),
        _capabilities(*ORDER),
        deadline=NOW + timedelta(minutes=1),
    )

    assert bundle.results == ()
    assert [(item.specialist, item.code) for item in bundle.failures] == [
        (SpecialistKind.METRICS, "ANALYSIS_FAILED"),
        (SpecialistKind.TRACE, "ANALYSIS_SCHEMA_INVALID"),
        (SpecialistKind.LOG, "MCP_PAYLOAD_TOO_LARGE"),
    ]
    assert "secret" not in repr(bundle).lower()


@pytest.mark.asyncio
async def test_global_deadline_cancels_pending_branches_and_waits_for_cleanup() -> None:
    started: list[SpecialistKind] = []
    cleaned_up: list[SpecialistKind] = []
    never_release = asyncio.Event()

    async def invoke(
        request: SpecialistRequest, kind: SpecialistKind, deadline: datetime
    ) -> SpecialistAnalysisResult:
        started.append(kind)
        if kind is SpecialistKind.METRICS:
            return _result(kind)
        try:
            await never_release.wait()
        finally:
            cleaned_up.append(kind)
        raise AssertionError("unreachable")

    live_now = datetime.now(UTC)
    bundle = await SpecialistAnalysisWorkflow(cast(BranchInvoker, invoke)).run(
        _context(),
        _capabilities(*ORDER),
        deadline=live_now + timedelta(milliseconds=50),
    )

    assert set(started) == set(ORDER)
    assert tuple(result.analysis.specialist for result in bundle.results) == (
        SpecialistKind.METRICS,
    )
    assert [(item.specialist, item.code) for item in bundle.failures] == [
        (SpecialistKind.TRACE, "ANALYSIS_TIMEOUT"),
        (SpecialistKind.LOG, "ANALYSIS_TIMEOUT"),
    ]
    assert set(cleaned_up) == {SpecialistKind.TRACE, SpecialistKind.LOG}


@pytest.mark.asyncio
async def test_expired_deadline_does_not_start_new_branch_attempts() -> None:
    calls: list[SpecialistKind] = []

    async def invoke(
        request: SpecialistRequest, kind: SpecialistKind, deadline: datetime
    ) -> SpecialistAnalysisResult:
        calls.append(kind)
        return _result(kind)

    bundle = await SpecialistAnalysisWorkflow(
        cast(BranchInvoker, invoke), clock=lambda: NOW
    ).run(
        _context(),
        _capabilities(SpecialistKind.METRICS, SpecialistKind.TRACE),
        deadline=NOW - timedelta(seconds=1),
    )

    assert calls == []
    assert [(item.specialist, item.code) for item in bundle.failures] == [
        (SpecialistKind.METRICS, "ANALYSIS_TIMEOUT"),
        (SpecialistKind.TRACE, "ANALYSIS_TIMEOUT"),
    ]


@pytest.mark.asyncio
async def test_transport_failure_retries_with_a_fresh_branch_attempt() -> None:
    attempts: list[object] = []

    async def invoke(
        request: SpecialistRequest, kind: SpecialistKind, deadline: datetime
    ) -> SpecialistAnalysisResult:
        attempts.append(object())
        if len(attempts) == 1:
            raise EvidenceToolError("MCP_TRANSPORT")
        return _result(kind)

    bundle = await SpecialistAnalysisWorkflow(
        cast(BranchInvoker, invoke), clock=lambda: NOW
    ).run(
        _context(),
        _capabilities(SpecialistKind.METRICS),
        deadline=NOW + timedelta(minutes=1),
    )

    assert len(attempts) == 2
    assert attempts[0] is not attempts[1]
    assert bundle.results == (_result(SpecialistKind.METRICS),)
    assert bundle.failures == ()


@pytest.mark.asyncio
async def test_exhausted_transport_failure_stops_after_two_attempts() -> None:
    calls = 0

    async def invoke(
        request: SpecialistRequest, kind: SpecialistKind, deadline: datetime
    ) -> SpecialistAnalysisResult:
        nonlocal calls
        calls += 1
        raise OSError("secret endpoint detail")

    bundle = await SpecialistAnalysisWorkflow(
        cast(BranchInvoker, invoke), clock=lambda: NOW
    ).run(
        _context(),
        _capabilities(SpecialistKind.METRICS),
        deadline=NOW + timedelta(minutes=1),
    )

    assert calls == 2
    assert [(item.specialist, item.code) for item in bundle.failures] == [
        (SpecialistKind.METRICS, "MCP_TRANSPORT")
    ]
    assert "secret" not in repr(bundle).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "NO_SAFE_MCP_CAPABILITY",
        "MCP_TIMEOUT",
        "MCP_RESULT_INVALID",
        "ANALYSIS_UNKNOWN_EVIDENCE",
        "ANALYSIS_FAILED",
    ],
)
async def test_non_transport_stable_failures_are_not_retried(
    code: StableSpecialistCode,
) -> None:
    calls = 0

    async def invoke(
        request: SpecialistRequest, kind: SpecialistKind, deadline: datetime
    ) -> SpecialistAnalysisResult:
        nonlocal calls
        calls += 1
        raise EvidenceToolError(code)

    bundle = await SpecialistAnalysisWorkflow(
        cast(BranchInvoker, invoke), clock=lambda: NOW
    ).run(
        _context(),
        _capabilities(SpecialistKind.METRICS),
        deadline=NOW + timedelta(minutes=1),
    )

    assert calls == 1
    assert [(item.specialist, item.code) for item in bundle.failures] == [
        (SpecialistKind.METRICS, code)
    ]


def test_analysis_bundle_models_forbid_extra_fields_and_unknown_failure_codes() -> None:
    with pytest.raises(ValidationError):
        SpecialistAnalysisResult.model_validate(
            {
                "analysis": _result(SpecialistKind.METRICS).analysis,
                "known_evidence": (REFERENCES[SpecialistKind.METRICS],),
                "raw_payload": {"secret": True},
            }
        )
    with pytest.raises(ValidationError):
        SpecialistAnalysisBundle(
            failures=(
                SpecialistFailure(
                    specialist=SpecialistKind.METRICS,
                    code="SPECIALIST_FAILED",
                ),
            )
        )
    with pytest.raises(ValidationError):
        SpecialistAnalysisBundle.model_validate(
            {"results": (), "failures": (), "raw_payload": b"secret"}
        )


def test_workflow_has_no_application_or_persistence_imports() -> None:
    workflow_path = (
        Path(__file__).resolve().parents[4]
        / "src/sre_rca_worker/agents/specialists/workflow.py"
    )
    tree = ast.parse(workflow_path.read_text(), filename=str(workflow_path))
    imported = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ]
    imported.extend(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not any(
        module == "sqlalchemy"
        or module.startswith(
            (
                "sqlalchemy.",
                "sre_rca_worker.application",
                "sre_rca_worker.persistence",
            )
        )
        for module in imported
    )
