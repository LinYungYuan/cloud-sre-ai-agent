from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest
from google.adk.models import (
    LlmCapabilities,  # pyright: ignore[reportAttributeAccessIssue]
)
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai.types import Content, Part
from pydantic import Field, PrivateAttr

from sre_rca_worker.agents.rca.adk_agent import AdkRcaAgent
from sre_rca_worker.agents.rca.models import IncidentContext
from sre_rca_worker.agents.rca.router import RuleRouter
from sre_rca_worker.agents.rca.synthesizer import RcaSynthesizer
from sre_rca_worker.agents.skills.loader import load_skill
from sre_rca_worker.agents.specialists.adk_agent import AdkSpecialistAgent
from sre_rca_worker.agents.specialists.base import SpecialistRequest
from sre_rca_worker.agents.specialists.log_agent import LogSpecialist
from sre_rca_worker.agents.specialists.metrics_agent import MetricsSpecialist
from sre_rca_worker.agents.specialists.trace_agent import TraceSpecialist
from sre_rca_worker.application.rca import evidence_tools as evidence_tools_module
from sre_rca_worker.application.rca.evidence_tools import (
    EvidenceReceipt,
    EvidenceToolError,
    EvidenceToolSession,
)
from sre_rca_worker.domain.evidence.analysis import (
    SpecialistAnalysisDraft,
    SpecialistObservation,
)
from sre_rca_worker.domain.evidence.models import EvidenceDraft, EvidenceReference
from sre_rca_worker.domain.rca.models import (
    EvidenceClaim,
    RcaHypothesis,
    RcaReportDraft,
)
from sre_rca_worker.integrations.mcp.models import (
    AllowedTool,
    CapabilitySet,
    CloudScope,
    DiscoveredTool,
    SpecialistKind,
)
from sre_rca_worker.persistence.repositories.rca import PersistedEvidence

DATASETS = Path(__file__).with_name("datasets")
NOW = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)
DEFINITIONS = (
    Path(__file__).resolve().parents[2] / "src/sre_rca_worker/agents/skills/definitions"
)
RCA_SKILL = load_skill(DEFINITIONS / "rca-analysis" / "SKILL.md")

pytestmark = [
    pytest.mark.filterwarnings(
        "ignore:BaseAgentConfig is deprecated and will be removed in future "
        "versions:DeprecationWarning"
    ),
    pytest.mark.filterwarnings(
        r"ignore:\[EXPERIMENTAL\] feature "
        r"FeatureName.JSON_SCHEMA_FOR_FUNC_DECL is enabled\.:UserWarning"
    ),
]

_LEGACY_DATASET_NAMES = (
    "aws-safe-scope.json",
    "gcp-safe-scope.json",
    "no-safe-scope.json",
)
_SPECIALIST_DATASET_NAMES = (
    "metrics-anomaly.json",
    "trace-critical-path.json",
    "log-exception-pattern.json",
)


def _load_case(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((DATASETS / name).read_text()))


def _window(case: dict[str, Any], field: str) -> datetime:
    if field not in case:
        return NOW - timedelta(minutes=15) if field == "windowStart" else NOW
    return datetime.fromisoformat(case[field])


def _reference(case: dict[str, Any], index: int = 0) -> EvidenceReference:
    ids = case.get("referenceIds")
    if ids is None:
        ids = [case["referenceId"]]
    return EvidenceReference(
        id=UUID(ids[index]),
    )


def _tool(kind: SpecialistKind) -> AllowedTool:
    return AllowedTool(
        name=f"{kind.value}_query",
        capability=f"{kind.value}.query",
        endpoint_identity=kind.value,
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
            },
            "required": ["project_id", "start_time", "end_time"],
            "additionalProperties": False,
        },
    )


def _context(case: dict[str, Any]) -> IncidentContext:
    return IncidentContext(
        incident_id=uuid4(),
        rca_run_id=uuid4(),
        alert_issue=case["alertIssue"],
        scope=CloudScope(
            provider=case["provider"],
            scope_id=case["scopeId"],
            safe=case["safe"],
        ),
        window_start=_window(case, "windowStart"),
        window_end=_window(case, "windowEnd"),
    )


def _capabilities(case: dict[str, Any]) -> CapabilitySet:
    return CapabilitySet(
        by_specialist={
            SpecialistKind(kind): (_tool(SpecialistKind(kind)),)
            for kind in case.get("available", ())
        }
    )


class _DeterministicLlm(BaseLlm):
    response: Content
    requests: list[LlmRequest] = Field(default_factory=list)

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(llm_request)
        yield LlmResponse(content=self.response, partial=False)


class _SequencedLlm(BaseLlm):
    responses: tuple[Content, ...]
    requests: list[LlmRequest] = Field(default_factory=list)
    evidence_supplier: Callable[[], EvidenceReceipt] | None = Field(
        default=None, exclude=True
    )
    required_marker: str | None = Field(default=None, exclude=True)
    consumed_chunks: list[str] = Field(default_factory=list)

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        del stream
        self.requests.append(llm_request)
        if self.evidence_supplier is not None:
            receipt = self.evidence_supplier()
            if not receipt.references or not receipt.first_chunks:
                raise AssertionError("injection model did not receive evidence")
            evidence_text = "".join(chunk.content for chunk in receipt.first_chunks)
            if (
                self.required_marker is None
                or self.required_marker not in evidence_text
            ):
                raise AssertionError("injection model did not consume evidence")
            self.consumed_chunks.append(evidence_text)
        yield LlmResponse(
            content=self.responses[len(self.requests) - 1],
            partial=False,
        )


def _install_fake_model(monkeypatch: pytest.MonkeyPatch, model: BaseLlm) -> None:
    from google.adk.models.registry import LLMRegistry

    monkeypatch.setattr(
        LLMRegistry,
        "new_llm",
        staticmethod(lambda _: model),
    )


def _request(case: dict[str, Any], kind: SpecialistKind) -> SpecialistRequest:
    context = _context(case)
    return SpecialistRequest(
        incident_id=context.incident_id,
        rca_run_id=context.rca_run_id,
        alert_issue=context.alert_issue,
        scope=context.scope,
        window_start=context.window_start,
        window_end=context.window_end,
        available_tools=(_tool(kind),),
    )


class _MemoryTransaction:
    def __init__(self, session: _MemorySession) -> None:
        self._session = session

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self._session.factory.rows.extend(self._session.pending)
        self._session.pending.clear()


class _MemorySession:
    def __init__(self, factory: _MemorySessionFactory) -> None:
        self.factory = factory
        self.pending: list[PersistedEvidence] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def begin(self) -> _MemoryTransaction:
        return _MemoryTransaction(self)


class _MemorySessionFactory:
    def __init__(self, reference_id: UUID | None = None) -> None:
        self.rows: list[PersistedEvidence] = []
        self.reference_id = reference_id

    def __call__(self) -> _MemorySession:
        return _MemorySession(self)


class _MemoryPersistEvidence:
    def __init__(self, session: _MemorySession) -> None:
        self._session = session

    async def save(
        self,
        rca_run_id: UUID,
        specialist_run_id: UUID | None,
        draft: EvidenceDraft,
    ) -> EvidenceReference:
        if specialist_run_id is None:
            raise AssertionError("specialist evidence must have an owner")
        reference = EvidenceReference(
            id=self._session.factory.reference_id or uuid4(),
        )
        self._session.pending.append(
            PersistedEvidence(
                reference=reference,
                rca_run_id=rca_run_id,
                specialist_run_id=specialist_run_id,
                evidence_type=draft.capability,
                source_endpoint=draft.endpoint_identity,
                tool_name=draft.tool,
                structured_data=draft.structured_json,
            )
        )
        return reference

    async def list_specialist_evidence(
        self, rca_run_id: UUID, specialist_run_id: UUID
    ) -> tuple[PersistedEvidence, ...]:
        return tuple(
            row
            for row in self._session.factory.rows
            if row.rca_run_id == rca_run_id
            and row.specialist_run_id == specialist_run_id
        )

    async def get_specialist_evidence(
        self,
        rca_run_id: UUID,
        specialist_run_id: UUID,
        evidence_id: UUID,
    ) -> PersistedEvidence | None:
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


class _CapturedDraftCollector:
    def __init__(self, kind: SpecialistKind, draft: EvidenceDraft) -> None:
        self.kind = kind
        self.draft = draft
        self.calls = 0

    async def collect_evidence_drafts(
        self, request: SpecialistRequest, deadline: datetime
    ) -> tuple[EvidenceDraft, ...]:
        del request, deadline
        self.calls += 1
        return (self.draft,)


class _EvidenceAwareLlm(BaseLlm):
    """Deterministic fake model that must consume actual persisted chunks."""

    requests: list[LlmRequest] = Field(default_factory=list)
    consumed_chunks: list[str] = Field(default_factory=list)
    _evidence_supplier: Callable[[], EvidenceReceipt] = PrivateAttr()
    _draft_builder: Callable[[EvidenceReceipt, str], SpecialistAnalysisDraft] = (
        PrivateAttr()
    )
    _required_marker: str = PrivateAttr()

    def __init__(
        self,
        *,
        evidence_supplier: Callable[[], EvidenceReceipt],
        draft_builder: Callable[[EvidenceReceipt, str], SpecialistAnalysisDraft],
        required_marker: str,
        **data: Any,
    ) -> None:
        super().__init__(**data)
        self._evidence_supplier = evidence_supplier
        self._draft_builder = draft_builder
        self._required_marker = required_marker

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        del stream
        self.requests.append(llm_request)
        receipt = self._evidence_supplier()
        if not receipt.references or not receipt.first_chunks:
            raise AssertionError("fake model did not receive persisted evidence")
        evidence_text = "".join(chunk.content for chunk in receipt.first_chunks)
        if self._required_marker not in evidence_text:
            raise AssertionError("fake model did not consume the expected evidence")
        self.consumed_chunks.append(evidence_text)
        draft = self._draft_builder(receipt, evidence_text)
        yield LlmResponse(
            content=Content(role="model", parts=[Part(text=draft.model_dump_json())]),
            partial=False,
        )


def _install_real_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    request: SpecialistRequest,
    kind: SpecialistKind,
    draft: EvidenceDraft,
    reference_id: UUID | None = None,
    chunk_chars: int = 8_000,
    max_chunks: int = 4,
    max_total_chars: int = 32_000,
    max_tool_calls: int = 5,
) -> tuple[EvidenceToolSession, _MemorySessionFactory, _CapturedDraftCollector]:
    persistence = _MemorySessionFactory(reference_id=reference_id)
    monkeypatch.setattr(
        evidence_tools_module, "PersistEvidence", _MemoryPersistEvidence
    )
    collector = _CapturedDraftCollector(kind, draft)
    tools = EvidenceToolSession(
        request=request,
        specialist_run_id=uuid4(),
        collector=collector,
        sessions=cast(Any, persistence),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        chunk_chars=chunk_chars,
        max_chunks=max_chunks,
        max_total_chars=max_total_chars,
        max_tool_calls=max_tool_calls,
    )
    return tools, persistence, collector


def _analysis(case: dict[str, Any], kind: SpecialistKind) -> SpecialistAnalysisDraft:
    reference = _reference(case)
    status = cast(Any, case.get("expectedStatus", "COMPLETE"))
    missing = (
        (cast(Any, case["expectedMissingEvidence"]),)
        if status == "PARTIAL" and case.get("expectedMissingEvidence")
        else ()
    )
    return SpecialistAnalysisDraft(
        specialist=kind,
        status=status,
        observations=(
            SpecialistObservation(
                statement=case["statement"],
                confidence=case["confidence"],
                relation=case["relation"],
                evidence=(reference,),
            ),
        ),
        missing_evidence=missing,
    )


def _analysis_from_evidence(
    kind: SpecialistKind,
    receipt: EvidenceReceipt,
    evidence_text: str,
) -> SpecialistAnalysisDraft:
    if not receipt.references:
        raise AssertionError("an evidence-aware model requires a reference")
    reference = receipt.references[0]
    parsed = json.loads(evidence_text)
    if kind is SpecialistKind.METRICS:
        values = [float(item["value"]) for item in parsed["series"]]
        if len(values) < 2 or values[-1] <= values[0]:
            raise AssertionError("metrics evidence did not contain an increase")
        statement = (
            f"CPU usage rose from {values[0]:g}% to {values[-1]:.0f}% "
            "during the approved window."
        )
        confidence = 0.94
    elif kind is SpecialistKind.TRACE:
        spans = parsed["spans"]
        services = [span["serviceName"] for span in spans]
        if not services or not all(span["criticalPath"] for span in spans):
            raise AssertionError("trace evidence did not contain a critical path")
        statement = (
            "The normalized checkout trace marks "
            + ", ".join(services[:-1])
            + f", and {services[-1]} spans as the critical path."
        )
        confidence = 0.91
    else:
        entries = parsed
        if len(entries) < 2:
            raise AssertionError("log evidence did not contain an ordered pair")
        timestamps = [entry["timestamp"] for entry in entries]
        if timestamps != sorted(timestamps):
            raise AssertionError("log evidence order was not preserved")
        short_timestamps = [timestamp.split("T", 1)[-1] for timestamp in timestamps]
        statement = (
            f"{entries[0]['message']} occurred at {short_timestamps[0]} before "
            f"{entries[1]['message']} at {short_timestamps[1]} in source order."
        )
        confidence = 0.89
    return SpecialistAnalysisDraft(
        specialist=kind,
        status="PARTIAL" if receipt.truncated else "COMPLETE",
        observations=(
            SpecialistObservation(
                statement=statement,
                confidence=confidence,
                relation="SUPPORTS",
                evidence=(reference,),
            ),
        ),
        missing_evidence=("ANALYSIS_INPUT_TRUNCATED",) if receipt.truncated else (),
    )


async def _run_specialist_model(
    monkeypatch: pytest.MonkeyPatch,
    case: dict[str, Any],
    kind: SpecialistKind,
    request: SpecialistRequest,
    draft: EvidenceDraft,
) -> tuple[SpecialistAnalysisDraft, _EvidenceAwareLlm, EvidenceReceipt]:
    tools, _, collector = _install_real_session(
        monkeypatch,
        request=request,
        kind=kind,
        draft=draft,
        reference_id=_reference(case).id,
    )
    receipt = await tools.collect_evidence()
    if not receipt.references or not receipt.first_chunks:
        raise AssertionError("specialist eval must persist bounded evidence")
    marker = {
        SpecialistKind.METRICS: '"series"',
        SpecialistKind.TRACE: '"spanId"',
        SpecialistKind.LOG: '"ConnectionPoolTimeout"',
    }[kind]
    model = _EvidenceAwareLlm(
        model=f"eval-{kind.value}",
        evidence_supplier=lambda: receipt,
        draft_builder=lambda current, text: _analysis_from_evidence(
            kind, current, text
        ),
        required_marker=marker,
    )
    _install_fake_model(monkeypatch, model)
    result = await AdkSpecialistAgent(
        kind=kind,
        model_name=f"eval-{kind.value}",
        skill_instruction=load_skill(
            DEFINITIONS / f"{kind.value}-analysis" / "SKILL.md"
        ).body,
    ).analyze(
        request=request,
        evidence_tools=tools,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    assert collector.calls == 1
    assert len(model.consumed_chunks) == 1
    return result, model, receipt


class _McpClient:
    def __init__(self, kind: SpecialistKind, payload: object) -> None:
        self.endpoint_identity = kind.value
        self.payload = payload
        self.calls: list[tuple[str, dict[str, object], datetime]] = []

    async def list_tools(self) -> tuple[DiscoveredTool, ...]:
        return ()

    async def call(
        self, tool_name: str, arguments: dict[str, object], deadline: datetime
    ) -> bytes:
        self.calls.append((tool_name, arguments, deadline))
        return json.dumps(self.payload).encode()


async def _collect_case(
    case: dict[str, Any], kind: SpecialistKind
) -> tuple[SpecialistRequest, EvidenceDraft, _McpClient]:
    payload = case.get("traceEvidence", case.get("structuredEvidence"))
    if payload is None and case.get("telemetryUnit"):
        unit = case["telemetryUnit"]
        payload = {
            "telemetry": unit * ((case["telemetryChars"] // len(unit)) + 1),
        }
    client = _McpClient(kind, payload)
    request = _request(case, kind)
    specialist = {
        SpecialistKind.METRICS: MetricsSpecialist,
        SpecialistKind.TRACE: TraceSpecialist,
        SpecialistKind.LOG: LogSpecialist,
    }[kind](lambda: client)
    drafts = await specialist.collect_evidence_drafts(
        request,
        _window(case, "windowEnd") + timedelta(minutes=1),
    )
    assert len(drafts) == 1
    return request, drafts[0], client


@pytest.mark.parametrize(
    "name",
    _LEGACY_DATASET_NAMES,
    ids=lambda name: Path(name).stem,
)
def test_route_and_partial_report_safety_dataset(name: str) -> None:
    case = _load_case(name)
    context = _context(case)
    plan = RuleRouter().route(context, _capabilities(case))
    assert [kind.value for kind in plan.selected] == case["expectedRoute"]
    assert all(value not in repr(plan) for value in case.get("forbidden", []))
    if case["expectedStatus"] == "PARTIAL":
        report = RcaSynthesizer().insufficient_evidence(provider=case["provider"])
        assert report.status == "PARTIAL"
        assert case["requiredPhrase"] in report.summary_zh_tw
    else:
        reference = EvidenceReference(id=uuid4())
        report = RcaSynthesizer().validate(
            RcaReportDraft(
                status="COMPLETE",
                summary_zh_tw="多個來源的時間序列支持主要假設。",
                hypotheses=(
                    RcaHypothesis(
                        statement=case["expectedLeadingHypothesis"],
                        confidence=case["expectedConfidence"],
                        claims=(
                            EvidenceClaim(
                                statement="延遲與連線池飽和同時發生",
                                relation="SUPPORTS",
                                evidence=(reference,),
                            ),
                        ),
                    ),
                ),
                missing_evidence=(),
                remediation=("由值班人員檢查連線池設定",),
                verification_steps=("確認延遲回復",),
            ),
            known_evidence=(reference,),
        )
        assert report.hypotheses[0].statement == case["expectedLeadingHypothesis"]
        assert report.hypotheses[0].confidence == case["expectedConfidence"]
        assert all(value not in repr(report) for value in case.get("forbidden", []))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    _SPECIALIST_DATASET_NAMES,
    ids=lambda name: Path(name).stem,
)
async def test_specialist_evaluation_fixtures_use_public_collection_and_adk_contracts(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    case = _load_case(name)
    kind = SpecialistKind(case["kind"])
    context = _context(case)
    plan = RuleRouter().route(context, _capabilities(case))
    assert plan.selected == (kind,)

    request, draft, client = await _collect_case(case, kind)
    structured = draft.structured_json
    serialized = json.dumps(structured, ensure_ascii=False)
    for forbidden in case.get("forbidden", []):
        assert forbidden not in serialized
    if kind is SpecialistKind.TRACE:
        normalized = cast(dict[str, Any], structured)
        assert normalized["schemaVersion"] == 1
        assert [span["spanId"] for span in normalized["spans"]] == [
            "span-gateway",
            "span-orders",
            "span-db",
        ]
        assert all(span["criticalPath"] for span in normalized["spans"])
    if kind is SpecialistKind.LOG:
        assert [
            entry["timestamp"] for entry in cast(list[dict[str, Any]], structured)
        ] == (case["expectedOrder"])
    expected_arguments = case.get(
        "expectedArguments",
        {
            "project_id": case["scopeId"],
            "start_time": case["windowStart"].replace("Z", "+00:00"),
            "end_time": case["windowEnd"].replace("Z", "+00:00"),
        },
    )
    assert client.calls[0][0:2] == (f"{kind.value}_query", expected_arguments)

    result, model, receipt = await _run_specialist_model(
        monkeypatch,
        case,
        kind,
        request,
        draft,
    )
    assert result.status == case["expectedStatus"]
    assert result.observations[0].statement == case["statement"]
    assert result.observations[0].evidence == receipt.references
    assert result.observations[0].relation == case["relation"]
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_conflicting_fixture_forwards_supports_and_contradicts_to_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _load_case("conflicting-observations.json")
    context = _context(case)
    capabilities = _capabilities({**case, "available": ["metrics", "trace"]})
    assert RuleRouter().route(context, capabilities).selected == (
        SpecialistKind.METRICS,
        SpecialistKind.TRACE,
    )
    references = tuple(_reference(case, index) for index in range(2))
    analyses = tuple(
        SpecialistAnalysisDraft(
            specialist=SpecialistKind(item["kind"]),
            status="COMPLETE",
            observations=(
                SpecialistObservation(
                    statement=item["statement"],
                    confidence=item["confidence"],
                    relation=item["relation"],
                    evidence=(references[item["referenceIndex"]],),
                ),
            ),
        )
        for item in case["analyses"]
    )
    root_response = RcaReportDraft(
        status="COMPLETE",
        summary_zh_tw="指標與追蹤資料呈現衝突，需保留兩種解釋。",
        hypotheses=(
            RcaHypothesis(
                statement="連線池與下游延遲皆需進一步確認",
                confidence=0.72,
                claims=(
                    EvidenceClaim(
                        statement="指標支持連線池飽和",
                        relation="SUPPORTS",
                        evidence=(references[0],),
                    ),
                    EvidenceClaim(
                        statement="追蹤資料反駁單一資料庫根因",
                        relation="CONTRADICTS",
                        evidence=(references[1],),
                    ),
                ),
            ),
        ),
        missing_evidence=(),
        remediation=("由值班人員比對兩份證據",),
        verification_steps=("確認下一個 incident window",),
    )
    model = _DeterministicLlm(
        model="eval-root",
        response=Content(
            role="model", parts=[Part(text=root_response.model_dump_json())]
        ),
    )
    _install_fake_model(monkeypatch, model)
    report = await AdkRcaAgent(
        model_name="eval-root",
        skill_instruction=RCA_SKILL.body,
    ).synthesize(
        alert_issue=case["alertIssue"],
        specialist_analyses=analyses,
        known_evidence=references,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    root_parts = model.requests[0].contents[-1].parts
    assert root_parts is not None
    prompt = root_parts[0].text
    assert prompt is not None
    assert "SUPPORTS" in prompt
    assert "CONTRADICTS" in prompt
    assert case["analyses"][0]["statement"] in prompt
    assert case["analyses"][1]["statement"] in prompt
    assert report.status == case["rootStatus"]
    assert len(report.hypotheses[0].claims) == 2


def _large_analysis_from_evidence(
    receipt: EvidenceReceipt,
    evidence_text: str,
) -> SpecialistAnalysisDraft:
    if not receipt.truncated or "cpu=95.2" not in evidence_text:
        raise AssertionError("large telemetry model did not consume bounded evidence")
    sample_count = evidence_text.count("cpu=95.2")
    return SpecialistAnalysisDraft(
        specialist=SpecialistKind.METRICS,
        status="PARTIAL",
        observations=(
            SpecialistObservation(
                statement=f"Bounded telemetry retained {sample_count} visible CPU samples.",
                confidence=0.7,
                relation="SUPPORTS",
                evidence=(receipt.references[0],),
            ),
        ),
        missing_evidence=("ANALYSIS_INPUT_TRUNCATED",),
    )


@pytest.mark.asyncio
async def test_large_telemetry_fixture_enforces_chunks_tool_budget_and_partial_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _load_case("large-telemetry.json")
    request, draft, client = await _collect_case(case, SpecialistKind.METRICS)
    tools, persistence, collector = _install_real_session(
        monkeypatch,
        request=request,
        kind=SpecialistKind.METRICS,
        draft=draft,
        reference_id=_reference(case).id,
        chunk_chars=case["chunkChars"],
        max_chunks=case["maxChunks"],
        max_total_chars=case["maxTotalChars"],
        max_tool_calls=case["maxToolCalls"],
    )
    receipt = await tools.collect_evidence()
    assert collector.calls == 1
    assert client.calls[0][0] == "metrics_query"
    assert len(persistence.rows) == 1
    assert persistence.rows[0].structured_data == draft.structured_json
    assert receipt.references == (_reference(case),)
    assert receipt.total_chunks == case["maxChunks"]
    assert receipt.truncated is True
    assert len(receipt.first_chunks) == 1
    assert all(
        len(chunk.content) <= case["chunkChars"] for chunk in receipt.first_chunks
    )
    assert all(chunk.truncated for chunk in receipt.first_chunks)

    chunks = tuple(
        [
            await tools.read_evidence_chunk(receipt.references[0].id, index)
            for index in range(receipt.total_chunks)
        ]
    )
    assert len(chunks) == case["maxChunks"]
    assert all(len(chunk.content) <= case["chunkChars"] for chunk in chunks)
    assert sum(len(chunk.content) for chunk in chunks) <= case["maxTotalChars"]
    assert all(chunk.truncated for chunk in chunks)
    with pytest.raises(EvidenceToolError) as raised:
        await tools.read_evidence_chunk(receipt.references[0].id, 0)
    assert raised.value.code == "ANALYSIS_FAILED"

    model = _EvidenceAwareLlm(
        model="eval-large",
        evidence_supplier=lambda: receipt,
        draft_builder=_large_analysis_from_evidence,
        required_marker="cpu=95.2",
    )
    _install_fake_model(monkeypatch, model)
    result = await AdkSpecialistAgent(
        kind=SpecialistKind.METRICS,
        model_name="eval-large",
        skill_instruction=load_skill(
            DEFINITIONS / "metrics-analysis" / "SKILL.md"
        ).body,
    ).analyze(
        request=request,
        evidence_tools=tools,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    assert result.status == case["expectedStatus"]
    assert result.missing_evidence == (case["expectedMissingEvidence"],)
    assert result.observations[0].evidence == receipt.references
    assert len(model.consumed_chunks) == 1


@pytest.mark.asyncio
async def test_prompt_injection_fixture_cannot_change_route_scope_window_or_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _load_case("prompt-injection.json")
    context = _context(case)
    plan = RuleRouter().route(context, _capabilities(case))
    assert plan.selected == (SpecialistKind.METRICS,)
    assert [kind.value for kind in plan.selected] == case["expectedRoute"]
    assert context.scope is not None
    assert context.scope.provider == case["provider"]
    assert context.scope.scope_id == case["expectedProjectId"]
    assert context.scope.safe is True

    client = _McpClient(SpecialistKind.METRICS, {"cpu": 95})
    request = _request(case, SpecialistKind.METRICS)
    drafts = await MetricsSpecialist(lambda: client).collect_evidence_drafts(
        request,
        _window(case, "windowEnd") + timedelta(minutes=1),
    )
    assert len(drafts) == 1
    assert client.calls[0][1] == case["expectedArguments"]
    assert drafts[0].input_scope.scope_id == case["expectedProjectId"]
    assert drafts[0].input_scope.provider == case["provider"]
    assert drafts[0].input_scope.safe is True
    assert drafts[0].request_window_start == _window(case, "windowStart")
    assert drafts[0].request_window_end == _window(case, "windowEnd")

    tools, _, collector = _install_real_session(
        monkeypatch,
        request=request,
        kind=SpecialistKind.METRICS,
        draft=drafts[0],
        reference_id=_reference(case).id,
    )
    receipt = await tools.collect_evidence()
    assert collector.calls == 1
    assert receipt.references == (_reference(case),)
    valid = SpecialistAnalysisDraft(
        specialist=SpecialistKind.METRICS,
        status="COMPLETE",
        observations=(
            SpecialistObservation(
                statement="The approved metrics evidence was analyzed.",
                confidence=0.8,
                relation="SUPPORTS",
                evidence=receipt.references,
            ),
        ),
    )
    malicious = {
        **json.loads(valid.model_dump_json()),
        "rootCause": "unsupported root cause from untrusted text",
        "remediation": "restart the service",
    }
    model = _SequencedLlm(
        model="eval-injection",
        responses=(
            Content(role="model", parts=[Part(text=json.dumps(malicious))]),
            Content(role="model", parts=[Part(text=valid.model_dump_json())]),
        ),
        evidence_supplier=lambda: receipt,
        required_marker='"cpu"',
    )
    _install_fake_model(monkeypatch, model)
    agent = AdkSpecialistAgent(
        kind=SpecialistKind.METRICS,
        model_name="eval-injection",
        skill_instruction=load_skill(
            DEFINITIONS / "metrics-analysis" / "SKILL.md"
        ).body,
    )
    result = await agent.analyze(
        request=request,
        evidence_tools=tools,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    first_parts = model.requests[0].contents[-1].parts
    correction_parts = model.requests[1].contents[-1].parts
    assert first_parts is not None
    assert correction_parts is not None
    first_prompt = first_parts[0].text
    correction_prompt = correction_parts[0].text
    assert first_prompt is not None
    assert correction_prompt is not None
    first_body = json.loads(first_prompt)
    approved = {key: value for key, value in first_body.items() if key != "alertValues"}
    assert first_body["alertValues"] == {
        "rawText": case["alertIssue"],
        "untrusted": True,
    }
    assert first_body["approvedScope"]["scopeId"] == case["expectedProjectId"]
    assert first_body["approvedTimeWindow"] == {
        "start": case["windowStart"],
        "end": case["windowEnd"],
    }
    assert all(
        value not in json.dumps(approved) for value in case["forbiddenProducedValues"]
    )
    correction_body = json.loads(correction_prompt)
    assert all(field not in correction_body for field in case["unsupportedFields"])
    assert "ANALYSIS_SCHEMA_INVALID" in correction_prompt
    assert result.status == "COMPLETE"
    assert all(value not in repr(result) for value in case["forbiddenProducedValues"])
    assert len(model.consumed_chunks) == 2
