from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID, uuid4

import pytest

from sre_rca_worker.agents.specialists.base import (
    SpecialistRequest,
    SpecialistResult,
)
from sre_rca_worker.application.rca import evidence_tools
from sre_rca_worker.application.rca.evidence_tools import (
    EvidenceToolError,
    EvidenceToolSession,
)
from sre_rca_worker.domain.evidence.models import (
    EvidenceDraft,
    EvidenceReference,
    Finding,
)
from sre_rca_worker.integrations.mcp.models import (
    AllowedTool,
    CloudScope,
    SpecialistKind,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _tool(index: int = 0) -> AllowedTool:
    return AllowedTool(
        name=f"metrics_query_{index}",
        capability="metrics.query",
        endpoint_identity="metrics",
        input_schema={
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    )


def _request(
    *,
    scope: CloudScope | None = None,
    tools: tuple[AllowedTool, ...] | None = None,
    rca_run_id: UUID | None = None,
) -> SpecialistRequest:
    return SpecialistRequest(
        incident_id=uuid4(),
        rca_run_id=rca_run_id or uuid4(),
        alert_issue="CPU high",
        scope=scope
        if scope is not None
        else CloudScope(provider="GCP", scope_id="project-a", safe=True),
        window_start=NOW - timedelta(minutes=15),
        window_end=NOW,
        available_tools=tools if tools is not None else (_tool(),),
    )


def _draft(*, structured: dict[str, object] | None = None) -> EvidenceDraft:
    scope = CloudScope(provider="GCP", scope_id="project-a", safe=True)
    return EvidenceDraft(
        endpoint_identity="metrics",
        capability="metrics.query",
        tool="metrics_query_0",
        input_scope=scope,
        normalized_scope=scope,
        observed_at=NOW,
        request_window_start=NOW - timedelta(minutes=15),
        request_window_end=NOW,
        window_start=NOW - timedelta(minutes=15),
        window_end=NOW,
        structured_json=structured or {"cpu": 85.23},
        raw_result=b'{"cpu":85.23}',
        content_type="application/json",
        input_sha256="a" * 64,
    )


class _Collector:
    kind = SpecialistKind.METRICS

    def __init__(self, drafts: tuple[EvidenceDraft, ...] = ()) -> None:
        self.calls = 0
        self.requests: list[SpecialistRequest] = []
        self._drafts = drafts
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def run(
        self, request: SpecialistRequest, deadline: datetime
    ) -> SpecialistResult:
        self.calls += 1
        self.requests.append(request)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        findings = tuple(
            Finding(summary="collected", confidence=0.5, evidence=(draft,))
            for draft in self._drafts
        )
        return SpecialistResult(specialist=self.kind, findings=findings)


class _Transaction:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            self._session.factory.rows.extend(self._session.pending)
        self._session.pending.clear()


class _Session:
    def __init__(self, factory: _Sessions) -> None:
        self.factory = factory
        self.pending: list[_StoredEvidence] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction(self)


class _Sessions:
    def __init__(self) -> None:
        self.rows: list[_StoredEvidence] = []

    def __call__(self) -> _Session:
        return _Session(self)


@dataclass(frozen=True)
class _StoredEvidence:
    reference: EvidenceReference
    rca_run_id: UUID
    specialist_run_id: UUID | None
    evidence_type: str
    source_endpoint: str
    tool_name: str
    structured_data: dict[str, object] | list[object]
    metadata: dict[str, object]


class _FakePersistEvidence:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def save(
        self,
        rca_run_id: UUID,
        specialist_run_id: UUID | None,
        draft: EvidenceDraft,
    ) -> EvidenceReference:
        reference = EvidenceReference(id=uuid4(), partition_timestamp=draft.observed_at)
        self._session.pending.append(
            _StoredEvidence(
                reference=reference,
                rca_run_id=rca_run_id,
                specialist_run_id=specialist_run_id,
                evidence_type=draft.capability,
                source_endpoint=draft.endpoint_identity,
                tool_name=draft.tool,
                structured_data=draft.structured_json,
                metadata={"contentType": draft.content_type},
            )
        )
        return reference

    async def list_specialist_evidence(
        self, rca_run_id: UUID, specialist_run_id: UUID
    ) -> tuple[_StoredEvidence, ...]:
        return tuple(
            row
            for row in self._session.factory.rows
            if row.rca_run_id == rca_run_id
            and row.specialist_run_id == specialist_run_id
        )

    async def get_specialist_evidence(
        self, rca_run_id: UUID, specialist_run_id: UUID, evidence_id: UUID
    ) -> _StoredEvidence | None:
        return next(
            (
                row
                for row in self._session.factory.rows
                if row.rca_run_id == rca_run_id
                and row.specialist_run_id == specialist_run_id
                and row.reference.id == evidence_id
            ),
            None,
        )


@pytest.fixture
def fake_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence_tools, "PersistEvidence", _FakePersistEvidence)


def _session(
    collector: _Collector,
    sessions: _Sessions,
    *,
    request: SpecialistRequest | None = None,
    specialist_run_id: UUID | None = None,
    deadline: datetime | None = None,
    max_tool_calls: int = 5,
    chunk_chars: int = 8_000,
    max_chunks: int = 4,
    max_total_chars: int = 32_000,
) -> EvidenceToolSession:
    return EvidenceToolSession(
        request=request or _request(),
        specialist_run_id=specialist_run_id or uuid4(),
        collector=collector,
        sessions=sessions,  # type: ignore[arg-type]
        deadline=deadline or NOW + timedelta(minutes=1),
        chunk_chars=chunk_chars,
        max_chunks=max_chunks,
        max_total_chars=max_total_chars,
        max_tool_calls=max_tool_calls,
        clock=lambda: NOW,
    )


def test_public_tool_signatures_do_not_accept_model_controlled_routing() -> None:
    assert tuple(inspect.signature(EvidenceToolSession.collect_evidence).parameters) == (
        "self",
    )
    assert tuple(
        inspect.signature(EvidenceToolSession.read_evidence_chunk).parameters
    ) == ("self", "evidence_id", "chunk_index")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "specialist_request",
    [
        _request(scope=CloudScope(provider="AWS", scope_id="account-a", safe=True)),
        _request(scope=CloudScope(provider="GCP", scope_id="project-a", safe=False)),
        _request(tools=()),
    ],
)
async def test_unsafe_or_unavailable_scope_does_not_invoke_collector(
    specialist_request: SpecialistRequest, fake_persistence: None
) -> None:
    collector = _Collector()

    receipt = await _session(
        collector, _Sessions(), request=specialist_request
    ).collect_evidence()

    assert receipt.specialist is SpecialistKind.METRICS
    assert receipt.references == ()
    assert collector.calls == 0


@pytest.mark.asyncio
async def test_sixth_model_tool_call_fails_with_stable_code(
    fake_persistence: None,
) -> None:
    tools = _session(_Collector(), _Sessions(), max_tool_calls=5)
    for _ in range(5):
        await tools.collect_evidence()

    with pytest.raises(EvidenceToolError) as raised:
        await tools.collect_evidence()

    assert raised.value.code == "ANALYSIS_FAILED"
    assert str(raised.value) == "ANALYSIS_FAILED"


@pytest.mark.asyncio
async def test_expired_deadline_rejects_collect_and_read_before_any_io(
    fake_persistence: None,
) -> None:
    class NoSessions:
        def __call__(self):
            raise AssertionError("expired tools must not access persistence")

    collector = _Collector()
    tools = _session(
        collector,
        NoSessions(),  # type: ignore[arg-type]
        deadline=NOW - timedelta(microseconds=1),
    )

    with pytest.raises(EvidenceToolError) as collect_error:
        await tools.collect_evidence()
    with pytest.raises(EvidenceToolError) as read_error:
        await tools.read_evidence_chunk(uuid4(), 0)

    assert collect_error.value.code == "ANALYSIS_TIMEOUT"
    assert read_error.value.code == "ANALYSIS_TIMEOUT"
    assert collector.calls == 0


@pytest.mark.asyncio
async def test_collect_caps_endpoint_bound_tools_at_five(
    fake_persistence: None,
) -> None:
    collector = _Collector()
    request = _request(tools=tuple(_tool(index) for index in range(6)))

    await _session(collector, _Sessions(), request=request).collect_evidence()

    assert len(collector.requests[0].available_tools) == 5


@pytest.mark.asyncio
async def test_parallel_collects_share_one_committed_receipt(
    fake_persistence: None,
) -> None:
    collector = _Collector((_draft(),))
    collector.entered = asyncio.Event()
    collector.release = asyncio.Event()
    tools = _session(collector, _Sessions())

    first = asyncio.create_task(tools.collect_evidence())
    await collector.entered.wait()
    second = asyncio.create_task(tools.collect_evidence())
    collector.release.set()
    first_receipt, second_receipt = await asyncio.gather(first, second)

    assert first_receipt == second_receipt
    assert collector.calls == 1
    assert tools.known_evidence == first_receipt.references


@pytest.mark.asyncio
async def test_receipt_has_one_first_chunk_per_evidence_and_truncation_signal(
    fake_persistence: None,
) -> None:
    collector = _Collector(
        (
            _draft(structured={"message": "abcdefghijklmnop"}),
            _draft(structured={"message": "qrstuvwxyz012345"}),
        )
    )
    tools = _session(
        collector,
        _Sessions(),
        chunk_chars=10,
        max_chunks=2,
        max_total_chars=20,
    )

    receipt = await tools.collect_evidence()

    assert len(receipt.references) == 2
    assert len(receipt.first_chunks) == 2
    assert receipt.total_chunks == 4
    assert receipt.truncated is True


@pytest.mark.asyncio
async def test_chunk_reads_rebuild_persisted_json_without_invoking_collector(
    fake_persistence: None,
) -> None:
    sessions = _Sessions()
    collector = _Collector()
    specialist_run_id = uuid4()
    request = _request()
    reference = EvidenceReference(id=uuid4(), partition_timestamp=NOW)
    sessions.rows.append(
        _StoredEvidence(
            reference=reference,
            rca_run_id=request.rca_run_id,
            specialist_run_id=specialist_run_id,
            evidence_type="metrics.query",
            source_endpoint="metrics",
            tool_name="metrics_query_0",
            structured_data={"message": "abcdefghijklmnop"},
            metadata={},
        )
    )
    tools = _session(
        collector,
        sessions,
        request=request,
        specialist_run_id=specialist_run_id,
        chunk_chars=10,
        max_chunks=2,
        max_total_chars=20,
    )

    chunk = await tools.read_evidence_chunk(reference.id, 1)

    assert chunk.chunk_index == 1
    assert chunk.content == ':"abcdefgh'
    assert chunk.truncated is True
    assert collector.calls == 0


@pytest.mark.asyncio
async def test_invalid_or_unowned_chunk_index_fails_closed(
    fake_persistence: None,
) -> None:
    tools = _session(_Collector(), _Sessions())

    with pytest.raises(EvidenceToolError) as raised:
        await tools.read_evidence_chunk(uuid4(), -1)

    assert raised.value.code == "ANALYSIS_UNKNOWN_EVIDENCE"
