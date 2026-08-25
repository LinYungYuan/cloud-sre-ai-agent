from __future__ import annotations

import json
from collections.abc import AsyncGenerator
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
from pydantic import Field

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
from sre_rca_worker.domain.evidence.chunking import (
    EvidenceChunk,
    build_evidence_chunks,
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
        partition_timestamp=_window(case, "windowEnd"),
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

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(llm_request)
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


class _StaticEvidenceTools:
    def __init__(
        self,
        reference: EvidenceReference,
        structured: dict[str, Any] | list[Any],
        *,
        truncated: bool = False,
        chunk_chars: int = 8_000,
        max_chunks: int = 4,
        max_total_chars: int = 32_000,
    ) -> None:
        self.known_evidence = (reference,)
        self.input_truncated = truncated
        self._chunks = build_evidence_chunks(
            reference,
            structured,
            chunk_chars=chunk_chars,
            max_chunks=max_chunks,
            max_total_chars=max_total_chars,
        )

    async def collect_evidence(self) -> EvidenceReceipt:
        return EvidenceReceipt(
            specialist=SpecialistKind.METRICS,
            references=self.known_evidence,
            first_chunks=tuple(chunk for chunk in self._chunks[:1]),
            total_chunks=len(self._chunks),
            truncated=self.input_truncated
            or any(chunk.truncated for chunk in self._chunks),
        )

    async def read_evidence_chunk(
        self, evidence_id: UUID, chunk_index: int
    ) -> EvidenceChunk:
        if evidence_id != self.known_evidence[0].id:
            raise EvidenceToolError("ANALYSIS_UNKNOWN_EVIDENCE")
        return self._chunks[chunk_index]


def _as_session(tools: _StaticEvidenceTools) -> EvidenceToolSession:
    return cast(EvidenceToolSession, tools)


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


async def _run_specialist_model(
    monkeypatch: pytest.MonkeyPatch,
    case: dict[str, Any],
    kind: SpecialistKind,
    structured: dict[str, Any] | list[Any],
    *,
    truncated: bool = False,
) -> tuple[SpecialistAnalysisDraft, _DeterministicLlm]:
    draft = _analysis(case, kind)
    model = _DeterministicLlm(
        model=f"eval-{kind.value}",
        response=Content(role="model", parts=[Part(text=draft.model_dump_json())]),
    )
    _install_fake_model(monkeypatch, model)
    tools = _StaticEvidenceTools(
        _reference(case),
        structured,
        truncated=truncated,
    )
    result = await AdkSpecialistAgent(
        kind=kind,
        model_name=f"eval-{kind.value}",
        skill_instruction=load_skill(
            DEFINITIONS / f"{kind.value}-analysis" / "SKILL.md"
        ).body,
    ).analyze(
        request=_request(case, kind),
        evidence_tools=_as_session(tools),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    return result, model


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
) -> tuple[dict[str, Any] | list[Any], _McpClient]:
    payload = case.get("traceEvidence", case.get("structuredEvidence"))
    client = _McpClient(kind, payload)
    specialist = {
        SpecialistKind.METRICS: MetricsSpecialist,
        SpecialistKind.TRACE: TraceSpecialist,
        SpecialistKind.LOG: LogSpecialist,
    }[kind](lambda: client)
    drafts = await specialist.collect_evidence_drafts(
        _request(case, kind),
        _window(case, "windowEnd") + timedelta(minutes=1),
    )
    assert len(drafts) == 1
    return drafts[0].structured_json, client


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
        reference = EvidenceReference(id=uuid4(), partition_timestamp=NOW)
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

    structured, client = await _collect_case(case, kind)
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

    result, model = await _run_specialist_model(monkeypatch, case, kind, structured)
    assert result.status == case["expectedStatus"]
    assert result.observations[0].statement == case["statement"]
    assert result.observations[0].evidence == (_reference(case),)
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


class _EmptySession:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _EmptyPersistence:
    def __init__(self, session: _EmptySession) -> None:
        del session

    async def list_specialist_evidence(
        self, rca_run_id: UUID, specialist_run_id: UUID
    ) -> tuple[object, ...]:
        return ()


class _NoopCollector:
    kind = SpecialistKind.METRICS

    async def collect_evidence_drafts(
        self, request: SpecialistRequest, deadline: datetime
    ) -> tuple[EvidenceDraft, ...]:
        return ()


@pytest.mark.asyncio
async def test_large_telemetry_fixture_enforces_chunks_tool_budget_and_partial_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _load_case("large-telemetry.json")
    payload = {
        "telemetry": case["telemetryUnit"]
        * ((case["telemetryChars"] // len(case["telemetryUnit"])) + 1)
    }
    reference = _reference(case)
    chunks = build_evidence_chunks(
        reference,
        payload,
        chunk_chars=case["chunkChars"],
        max_chunks=case["maxChunks"],
        max_total_chars=case["maxTotalChars"],
    )
    assert len(chunks) <= case["maxChunks"]
    assert sum(len(chunk.content) for chunk in chunks) <= case["maxTotalChars"]
    assert all(len(chunk.content) <= case["chunkChars"] for chunk in chunks)
    assert all(chunk.truncated for chunk in chunks)

    monkeypatch.setattr(evidence_tools_module, "PersistEvidence", _EmptyPersistence)
    tools = EvidenceToolSession(
        request=_request(case, SpecialistKind.METRICS),
        specialist_run_id=uuid4(),
        collector=_NoopCollector(),
        sessions=lambda: _EmptySession(),  # type: ignore[arg-type]
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        chunk_chars=case["chunkChars"],
        max_chunks=case["maxChunks"],
        max_total_chars=case["maxTotalChars"],
        max_tool_calls=case["maxToolCalls"],
    )
    for _ in range(case["maxToolCalls"]):
        await tools.collect_evidence()
    with pytest.raises(EvidenceToolError) as raised:
        await tools.collect_evidence()
    assert raised.value.code == "ANALYSIS_FAILED"

    draft = SpecialistAnalysisDraft(
        specialist=SpecialistKind.METRICS,
        status="PARTIAL",
        observations=(
            SpecialistObservation(
                statement="Large telemetry was bounded before analysis.",
                confidence=0.7,
                relation="SUPPORTS",
                evidence=(reference,),
            ),
        ),
        missing_evidence=("ANALYSIS_INPUT_TRUNCATED",),
    )
    model = _DeterministicLlm(
        model="eval-large",
        response=Content(role="model", parts=[Part(text=draft.model_dump_json())]),
    )
    _install_fake_model(monkeypatch, model)
    result = await AdkSpecialistAgent(
        kind=SpecialistKind.METRICS,
        model_name="eval-large",
        skill_instruction=load_skill(
            DEFINITIONS / "metrics-analysis" / "SKILL.md"
        ).body,
    ).analyze(
        request=_request(case, SpecialistKind.METRICS),
        evidence_tools=_as_session(
            _StaticEvidenceTools(
                reference,
                payload,
                truncated=True,
                chunk_chars=case["chunkChars"],
                max_chunks=case["maxChunks"],
                max_total_chars=case["maxTotalChars"],
            )
        ),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    assert result.status == case["expectedStatus"]
    assert result.missing_evidence == (case["expectedMissingEvidence"],)


@pytest.mark.asyncio
async def test_prompt_injection_fixture_cannot_change_route_scope_window_or_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _load_case("prompt-injection.json")
    context = _context(case)
    plan = RuleRouter().route(context, _capabilities(case))
    assert [kind.value for kind in plan.selected] == case["expectedRoute"]

    client = _McpClient(SpecialistKind.METRICS, {"cpu": 95})
    drafts = await MetricsSpecialist(lambda: client).collect_evidence_drafts(
        _request(case, SpecialistKind.METRICS),
        _window(case, "windowEnd") + timedelta(minutes=1),
    )
    assert len(drafts) == 1
    assert client.calls[0][1] == case["expectedArguments"]
    assert drafts[0].input_scope.scope_id == case["expectedProjectId"]
    assert drafts[0].request_window_start == _window(case, "windowStart")
    assert drafts[0].request_window_end == _window(case, "windowEnd")

    reference = _reference(case)
    valid = SpecialistAnalysisDraft(
        specialist=SpecialistKind.METRICS,
        status="COMPLETE",
        observations=(
            SpecialistObservation(
                statement="The approved metrics evidence was analyzed.",
                confidence=0.8,
                relation="SUPPORTS",
                evidence=(reference,),
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
        request=_request(case, SpecialistKind.METRICS),
        evidence_tools=_as_session(
            _StaticEvidenceTools(reference, drafts[0].structured_json)
        ),
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
