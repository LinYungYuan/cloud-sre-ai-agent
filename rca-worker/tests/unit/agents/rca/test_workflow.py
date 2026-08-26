import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sre_rca_worker.agents.rca.models import IncidentContext
from sre_rca_worker.agents.rca.workflow import RcaWorkflow
from sre_rca_worker.agents.specialists.base import (
    McpPayloadTooLargeError,
    McpResultInvalidError,
    SpecialistRequest,
    SpecialistResult,
)
from sre_rca_worker.integrations.mcp.models import (
    AllowedTool,
    CapabilitySet,
    CloudScope,
    SpecialistKind,
)


class FakeSpecialist:
    def __init__(
        self,
        kind: SpecialistKind,
        started: list[SpecialistKind],
        barrier: asyncio.Event,
        *,
        failure: Exception | None = None,
        transient_failures: int = 0,
    ) -> None:
        self.kind = kind
        self.started = started
        self.barrier = barrier
        self.failure = failure
        self.transient_failures = transient_failures
        self.calls = 0

    async def run(
        self, request: SpecialistRequest, deadline: datetime
    ) -> SpecialistResult:
        self.calls += 1
        self.started.append(self.kind)
        await self.barrier.wait()
        if self.calls <= self.transient_failures:
            raise ConnectionError("temporary endpoint outage with secret details")
        if self.failure:
            raise self.failure
        return SpecialistResult(specialist=self.kind)


def _tool(kind: SpecialistKind) -> AllowedTool:
    return AllowedTool(
        name=f"{kind.value}_query",
        capability=f"{kind.value}.query",
        endpoint_identity=kind.value,
        input_schema={"type": "object"},
    )


def _context(now: datetime) -> IncidentContext:
    return IncidentContext(
        incident_id=uuid4(),
        rca_run_id=uuid4(),
        alert_issue="untrusted alert value",
        scope=CloudScope(provider="GCP", scope_id="p", safe=True),
        window_start=now - timedelta(minutes=15),
        window_end=now,
    )


@pytest.mark.asyncio
async def test_workflow_starts_all_specialists_and_keeps_partial_results() -> None:
    now = datetime.now(UTC)
    started: list[SpecialistKind] = []
    barrier = asyncio.Event()
    specialists = {
        kind: FakeSpecialist(
            kind,
            started,
            barrier,
            failure=RuntimeError("secret transport details")
            if kind is SpecialistKind.TRACE
            else None,
        )
        for kind in SpecialistKind
    }
    capabilities = CapabilitySet(
        by_specialist={kind: (_tool(kind),) for kind in SpecialistKind}
    )
    task = asyncio.create_task(
        RcaWorkflow(specialists).run(
            _context(now), capabilities, deadline=now + timedelta(seconds=5)
        )
    )
    while len(started) < 3:
        await asyncio.sleep(0)
    assert set(started) == set(SpecialistKind)
    barrier.set()
    bundle = await task

    assert tuple(result.specialist for result in bundle.results) == (
        SpecialistKind.METRICS,
        SpecialistKind.LOG,
    )
    assert [(item.specialist, item.code) for item in bundle.failures] == [
        (SpecialistKind.TRACE, "SPECIALIST_FAILED")
    ]
    assert "secret" not in repr(bundle)


@pytest.mark.asyncio
async def test_expired_deadline_or_empty_route_invokes_nothing() -> None:
    now = datetime.now(UTC)
    calls: list[SpecialistKind] = []
    barrier = asyncio.Event()
    empty = CapabilitySet(by_specialist={})
    specialists = {
        kind: FakeSpecialist(kind, calls, barrier) for kind in SpecialistKind
    }
    workflow = RcaWorkflow(specialists)

    assert (
        await workflow.run(_context(now), empty, deadline=now + timedelta(seconds=1))
    ).results == ()
    with pytest.raises(TimeoutError):
        await workflow.run(
            _context(now),
            CapabilitySet(
                by_specialist={SpecialistKind.METRICS: (_tool(SpecialistKind.METRICS),)}
            ),
            deadline=now - timedelta(seconds=1),
        )
    assert calls == []


@pytest.mark.asyncio
async def test_workflow_retries_transient_transport_once_but_not_policy_failure() -> (
    None
):
    now = datetime.now(UTC)
    started: list[SpecialistKind] = []
    barrier = asyncio.Event()
    barrier.set()
    metrics = FakeSpecialist(
        SpecialistKind.METRICS, started, barrier, transient_failures=1
    )
    trace = FakeSpecialist(
        SpecialistKind.TRACE,
        started,
        barrier,
        failure=ValueError("permanent schema failure"),
    )
    bundle = await RcaWorkflow(
        {
            SpecialistKind.METRICS: metrics,
            SpecialistKind.TRACE: trace,
        }
    ).run(
        _context(now),
        CapabilitySet(
            by_specialist={
                SpecialistKind.METRICS: (_tool(SpecialistKind.METRICS),),
                SpecialistKind.TRACE: (_tool(SpecialistKind.TRACE),),
            }
        ),
        deadline=now + timedelta(seconds=5),
    )

    assert metrics.calls == 2
    assert trace.calls == 1
    assert tuple(item.specialist for item in bundle.results) == (
        SpecialistKind.METRICS,
    )
    assert [(item.specialist, item.code) for item in bundle.failures] == [
        (SpecialistKind.TRACE, "SPECIALIST_VALIDATION")
    ]


@pytest.mark.asyncio
async def test_exhausted_transport_retry_has_a_distinct_safe_failure_code() -> None:
    now = datetime.now(UTC)
    barrier = asyncio.Event()
    barrier.set()
    metrics = FakeSpecialist(SpecialistKind.METRICS, [], barrier, transient_failures=2)

    bundle = await RcaWorkflow({SpecialistKind.METRICS: metrics}).run(
        _context(now),
        CapabilitySet(
            by_specialist={SpecialistKind.METRICS: (_tool(SpecialistKind.METRICS),)}
        ),
        deadline=now + timedelta(seconds=5),
    )

    assert metrics.calls == 2
    assert [(item.specialist, item.code) for item in bundle.failures] == [
        (SpecialistKind.METRICS, "SPECIALIST_TRANSPORT")
    ]


@pytest.mark.asyncio
async def test_invalid_mcp_result_is_terminal_and_not_retried_as_transport() -> None:
    now = datetime.now(UTC)
    barrier = asyncio.Event()
    barrier.set()
    metrics = FakeSpecialist(
        SpecialistKind.METRICS,
        [],
        barrier,
        failure=McpResultInvalidError(),
    )

    bundle = await RcaWorkflow({SpecialistKind.METRICS: metrics}).run(
        _context(now),
        CapabilitySet(
            by_specialist={SpecialistKind.METRICS: (_tool(SpecialistKind.METRICS),)}
        ),
        deadline=now + timedelta(seconds=5),
    )

    assert metrics.calls == 1
    assert [(item.specialist, item.code) for item in bundle.failures] == [
        (SpecialistKind.METRICS, "MCP_RESULT_INVALID")
    ]


@pytest.mark.asyncio
async def test_oversized_mcp_result_keeps_payload_failure_code() -> None:
    now = datetime.now(UTC)
    barrier = asyncio.Event()
    barrier.set()
    metrics = FakeSpecialist(
        SpecialistKind.METRICS,
        [],
        barrier,
        failure=McpPayloadTooLargeError("secret response metadata"),
    )

    bundle = await RcaWorkflow({SpecialistKind.METRICS: metrics}).run(
        _context(now),
        CapabilitySet(
            by_specialist={SpecialistKind.METRICS: (_tool(SpecialistKind.METRICS),)}
        ),
        deadline=now + timedelta(seconds=5),
    )

    assert metrics.calls == 1
    assert [(item.specialist, item.code) for item in bundle.failures] == [
        (SpecialistKind.METRICS, "MCP_PAYLOAD_TOO_LARGE")
    ]
