import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sre_rca_worker.agents.rca.models import IncidentContext
from sre_rca_worker.agents.rca.workflow import RcaWorkflow
from sre_rca_worker.agents.specialists.base import SpecialistRequest, SpecialistResult
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
    ) -> None:
        self.kind = kind
        self.started = started
        self.barrier = barrier
        self.failure = failure

    async def run(
        self, request: SpecialistRequest, deadline: datetime
    ) -> SpecialistResult:
        self.started.append(self.kind)
        await self.barrier.wait()
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
