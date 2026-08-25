from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID, uuid4

import pytest

from sre_rca_worker.agents.specialists import base as specialist_base
from sre_rca_worker.agents.specialists.base import (
    McpSpecialist,
    SpecialistRequest,
)
from sre_rca_worker.agents.specialists.metrics_agent import MetricsSpecialist
from sre_rca_worker.application.rca import evidence_tools
from sre_rca_worker.application.rca.evidence_tools import (
    EvidenceCollector,
    EvidenceToolError,
    EvidenceToolSession,
)
from sre_rca_worker.domain.evidence.models import (
    EvidenceDraft,
    EvidenceReference,
)
from sre_rca_worker.integrations.mcp.models import (
    AllowedTool,
    CloudScope,
    DiscoveredTool,
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

    def __init__(
        self,
        drafts: tuple[EvidenceDraft, ...] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        self.calls = 0
        self.requests: list[SpecialistRequest] = []
        self._drafts = drafts
        self._error = error
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def collect_evidence_drafts(
        self, request: SpecialistRequest, deadline: datetime
    ) -> tuple[EvidenceDraft, ...]:
        self.calls += 1
        self.requests.append(request)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self._error is not None:
            raise self._error
        return self._drafts


class _Transaction:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            self._session.factory.rows.extend(self._session.pending)
            self._session.factory.on_commit()
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
        self.calls = 0
        self.save_calls = 0
        self.on_list: Callable[[], None] = lambda: None
        self.on_get: Callable[[], None] = lambda: None
        self.on_save: Callable[[], None] = lambda: None
        self.on_commit: Callable[[], None] = lambda: None

    def __call__(self) -> _Session:
        self.calls += 1
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
        self._session.factory.save_calls += 1
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
        self._session.factory.on_save()
        return reference

    async def list_specialist_evidence(
        self, rca_run_id: UUID, specialist_run_id: UUID
    ) -> tuple[_StoredEvidence, ...]:
        self._session.factory.on_list()
        return tuple(
            row
            for row in self._session.factory.rows
            if row.rca_run_id == rca_run_id
            and row.specialist_run_id == specialist_run_id
        )

    async def get_specialist_evidence(
        self, rca_run_id: UUID, specialist_run_id: UUID, evidence_id: UUID
    ) -> _StoredEvidence | None:
        self._session.factory.on_get()
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
    collector: EvidenceCollector,
    sessions: _Sessions,
    *,
    request: SpecialistRequest | None = None,
    specialist_run_id: UUID | None = None,
    deadline: datetime | None = None,
    max_tool_calls: int = 5,
    chunk_chars: int = 8_000,
    max_chunks: int = 4,
    max_total_chars: int = 32_000,
    clock: Callable[[], datetime] | None = None,
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
        clock=clock or (lambda: NOW),
    )


class _MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def expire(self) -> None:
        self.now = NOW + timedelta(minutes=1)


def _stored(
    request: SpecialistRequest,
    specialist_run_id: UUID,
    *,
    content: dict[str, object] | None = None,
) -> _StoredEvidence:
    return _StoredEvidence(
        reference=EvidenceReference(id=uuid4(), partition_timestamp=NOW),
        rca_run_id=request.rca_run_id,
        specialist_run_id=specialist_run_id,
        evidence_type="metrics.query",
        source_endpoint="metrics",
        tool_name="metrics_query_0",
        structured_data=content or {"cpu": 85.23},
        metadata={},
    )


def test_public_tool_signatures_do_not_accept_model_controlled_routing() -> None:
    assert tuple(
        inspect.signature(EvidenceToolSession.collect_evidence).parameters
    ) == ("self",)
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
@pytest.mark.parametrize(
    "specialist_request",
    [
        _request(scope=CloudScope(provider="AWS", scope_id="account-a", safe=True)),
        _request(scope=CloudScope(provider="GCP", scope_id="project-a", safe=False)),
        _request(tools=()),
    ],
)
async def test_preflight_rejects_unsafe_or_empty_requests_before_db_reuse(
    specialist_request: SpecialistRequest, fake_persistence: None
) -> None:
    collector = _Collector()
    sessions = _Sessions()
    specialist_run_id = uuid4()
    sessions.rows.append(_stored(specialist_request, specialist_run_id))

    receipt = await _session(
        collector,
        sessions,
        request=specialist_request,
        specialist_run_id=specialist_run_id,
    ).collect_evidence()

    assert receipt.references == ()
    assert sessions.calls == 0
    assert collector.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tools",
    [
        (
            AllowedTool(
                name="metrics_query",
                capability="metrics.query",
                endpoint_identity="trace",
                input_schema=_tool().input_schema,
            ),
        ),
        (
            AllowedTool(
                name="metrics_query",
                capability="metrics.query.extra",
                endpoint_identity="metrics",
                input_schema=_tool().input_schema,
            ),
        ),
        (
            AllowedTool(
                name="metrics_query",
                capability="metrics.query",
                endpoint_identity="metrics",
                input_schema={"type": "not-a-json-schema-type"},
            ),
        ),
        (_tool(), _tool()),
    ],
)
async def test_invalid_allowed_tools_fail_closed_before_db_or_mcp(
    tools: tuple[AllowedTool, ...], fake_persistence: None
) -> None:
    collector = _Collector()
    sessions = _Sessions()

    with pytest.raises(EvidenceToolError) as raised:
        await _session(
            collector, sessions, request=_request(tools=tools)
        ).collect_evidence()

    assert raised.value.code == "ANALYSIS_FAILED"
    assert sessions.calls == 0
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
async def test_collector_failure_is_terminal_for_the_session(
    fake_persistence: None,
) -> None:
    collector = _Collector(error=ConnectionError("sensitive transport detail"))
    tools = _session(collector, _Sessions())

    failures = []
    for _ in range(2):
        with pytest.raises(EvidenceToolError) as raised:
            await tools.collect_evidence()
        failures.append(raised.value)

    assert [failure.code for failure in failures] == [
        "MCP_TRANSPORT",
        "MCP_TRANSPORT",
    ]
    assert all("sensitive" not in str(failure) for failure in failures)
    assert collector.calls == 1


@pytest.mark.asyncio
async def test_cached_collector_failure_still_consumes_model_tool_budget(
    fake_persistence: None,
) -> None:
    collector = _Collector(error=ConnectionError("sensitive transport detail"))
    sessions = _Sessions()
    tools = _session(collector, sessions, max_tool_calls=2)

    codes = []
    for _ in range(3):
        with pytest.raises(EvidenceToolError) as raised:
            await tools.collect_evidence()
        codes.append(raised.value.code)

    assert codes == ["MCP_TRANSPORT", "MCP_TRANSPORT", "ANALYSIS_FAILED"]
    assert collector.calls == 1
    assert sessions.calls == 1


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
async def test_deadline_crossed_during_reuse_lookup_returns_no_content(
    fake_persistence: None,
) -> None:
    clock = _MutableClock()
    request = _request()
    specialist_run_id = uuid4()
    sessions = _Sessions()
    sessions.rows.append(_stored(request, specialist_run_id))
    sessions.on_list = clock.expire
    collector = _Collector()

    with pytest.raises(EvidenceToolError) as raised:
        await _session(
            collector,
            sessions,
            request=request,
            specialist_run_id=specialist_run_id,
            clock=clock,
        ).collect_evidence()

    assert raised.value.code == "ANALYSIS_TIMEOUT"
    assert collector.calls == 0


@pytest.mark.asyncio
async def test_deadline_crossed_after_insert_stops_later_inserts_and_rolls_back(
    fake_persistence: None,
) -> None:
    clock = _MutableClock()
    sessions = _Sessions()
    sessions.on_save = clock.expire
    tools = _session(
        _Collector((_draft(), _draft(structured={"cpu": 90.0}))),
        sessions,
        clock=clock,
    )

    with pytest.raises(EvidenceToolError) as raised:
        await tools.collect_evidence()

    assert raised.value.code == "ANALYSIS_TIMEOUT"
    assert sessions.save_calls == 1
    assert sessions.rows == []


@pytest.mark.asyncio
async def test_deadline_crossed_during_commit_never_returns_receipt(
    fake_persistence: None,
) -> None:
    clock = _MutableClock()
    sessions = _Sessions()
    sessions.on_commit = clock.expire
    tools = _session(_Collector((_draft(),)), sessions, clock=clock)

    with pytest.raises(EvidenceToolError) as raised:
        await tools.collect_evidence()

    assert raised.value.code == "ANALYSIS_TIMEOUT"
    assert len(sessions.rows) == 1


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
async def test_direct_collection_never_calls_legacy_run_or_constructs_finding(
    fake_persistence: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class Client:
        endpoint_identity = "metrics"

        async def list_tools(self) -> tuple[DiscoveredTool, ...]:
            return ()

        async def call(
            self,
            tool_name: str,
            arguments: dict[str, object],
            deadline: datetime,
        ) -> bytes:
            nonlocal calls
            calls += 1
            return b'{"cpu":85.23}'

    async def legacy_run_forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("ACTIVE/SHADOW collection must not call legacy run()")

    def finding_forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("ACTIVE/SHADOW collection must not construct Finding")

    monkeypatch.setattr(McpSpecialist, "run", legacy_run_forbidden)
    monkeypatch.setattr(specialist_base, "Finding", finding_forbidden)

    receipt = await _session(
        MetricsSpecialist(lambda: Client()),
        _Sessions(),
    ).collect_evidence()

    assert len(receipt.references) == 1
    assert calls == 1


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
    assert tools.input_truncated is True


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
async def test_deadline_crossed_during_chunk_lookup_returns_no_content(
    fake_persistence: None,
) -> None:
    clock = _MutableClock()
    sessions = _Sessions()
    collector = _Collector()
    specialist_run_id = uuid4()
    request = _request()
    record = _stored(request, specialist_run_id)
    sessions.rows.append(record)
    sessions.on_get = clock.expire
    tools = _session(
        collector,
        sessions,
        request=request,
        specialist_run_id=specialist_run_id,
        clock=clock,
    )

    with pytest.raises(EvidenceToolError) as raised:
        await tools.read_evidence_chunk(record.reference.id, 0)

    assert raised.value.code == "ANALYSIS_TIMEOUT"
    assert collector.calls == 0


@pytest.mark.asyncio
async def test_invalid_or_unowned_chunk_index_fails_closed(
    fake_persistence: None,
) -> None:
    tools = _session(_Collector(), _Sessions())

    with pytest.raises(EvidenceToolError) as raised:
        await tools.read_evidence_chunk(uuid4(), -1)

    assert raised.value.code == "ANALYSIS_UNKNOWN_EVIDENCE"
