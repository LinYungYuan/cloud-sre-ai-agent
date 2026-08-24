from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from google.adk.models import (
    LlmCapabilities,  # pyright: ignore[reportAttributeAccessIssue]
)
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai.types import Content, Part
from pydantic import Field

from sre_rca_worker.agents.skills.loader import load_skill
from sre_rca_worker.agents.specialists.adk_agent import AdkSpecialistAgent
from sre_rca_worker.agents.specialists.base import SpecialistRequest
from sre_rca_worker.agents.specialists.validator import (
    SpecialistAnalysisValidationError,
)
from sre_rca_worker.application.rca.evidence_tools import (
    EvidenceReceipt,
    EvidenceToolError,
    EvidenceToolSession,
)
from sre_rca_worker.domain.evidence.analysis import (
    SpecialistAnalysisDraft,
    SpecialistObservation,
)
from sre_rca_worker.domain.evidence.chunking import EvidenceChunk
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.integrations.mcp.models import (
    AllowedTool,
    CloudScope,
    SpecialistKind,
)

NOW = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
OWNED = EvidenceReference(
    id=UUID("00000000-0000-0000-0000-000000000001"),
    partition_timestamp=NOW,
)
UNKNOWN = EvidenceReference(
    id=UUID("00000000-0000-0000-0000-000000000099"),
    partition_timestamp=NOW,
)
DEFINITIONS = (
    Path(__file__).resolve().parents[4] / "src/sre_rca_worker/agents/skills/definitions"
)
METRICS_SKILL = load_skill(DEFINITIONS / "metrics-analysis" / "SKILL.md")
SKILLS = {
    kind: load_skill(DEFINITIONS / f"{kind.value}-analysis" / "SKILL.md")
    for kind in SpecialistKind
}


def _request() -> SpecialistRequest:
    return SpecialistRequest(
        incident_id=UUID("10000000-0000-0000-0000-000000000001"),
        rca_run_id=UUID("20000000-0000-0000-0000-000000000001"),
        alert_issue="CPU saturation is above the alert threshold.",
        scope=CloudScope(provider="GCP", scope_id="project-a", safe=True),
        window_start=NOW - timedelta(minutes=15),
        window_end=NOW,
        available_tools=(
            AllowedTool(
                name="private_metrics_transport_tool",
                capability="metrics.query",
                endpoint_identity="metrics",
                input_schema={
                    "type": "object",
                    "properties": {"private_project_argument": {"type": "string"}},
                    "additionalProperties": False,
                },
            ),
        ),
    )


def _draft(reference: EvidenceReference = OWNED) -> SpecialistAnalysisDraft:
    return SpecialistAnalysisDraft(
        specialist=SpecialistKind.METRICS,
        status="COMPLETE",
        observations=(
            SpecialistObservation(
                statement="CPU usage rose during the approved incident window.",
                confidence=0.9,
                relation="SUPPORTS",
                evidence=(reference,),
            ),
        ),
    )


class _EvidenceTools:
    def __init__(self, *, truncated: bool = False) -> None:
        self.known_evidence = (OWNED,)
        self.input_truncated = truncated
        self.collect_calls = 0
        self.read_calls: list[tuple[UUID, int]] = []
        self.chunk = EvidenceChunk(
            reference=OWNED,
            chunk_index=0,
            chunk_count=1,
            content='{"cpu":95}',
            truncated=truncated,
        )

    async def collect_evidence(self) -> EvidenceReceipt:
        self.collect_calls += 1
        return EvidenceReceipt(
            specialist=SpecialistKind.METRICS,
            references=(OWNED,),
            first_chunks=(self.chunk,),
            total_chunks=1,
            truncated=self.input_truncated,
        )

    async def read_evidence_chunk(
        self, evidence_id: UUID, chunk_index: int
    ) -> EvidenceChunk:
        self.read_calls.append((evidence_id, chunk_index))
        return self.chunk


def _as_session(tools: _EvidenceTools) -> EvidenceToolSession:
    return cast(EvidenceToolSession, tools)


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


def _install_fake_model(
    monkeypatch: pytest.MonkeyPatch, fake_model: _DeterministicLlm
) -> None:
    from google.adk.models.registry import LLMRegistry

    monkeypatch.setattr(
        LLMRegistry,
        "new_llm",
        staticmethod(lambda model_name: fake_model),
    )


class _ResponseAgent(AdkSpecialistAgent):
    def __init__(self, responses: tuple[str, ...], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.responses = iter(responses)
        self.prompts: list[str] = []
        self.deadlines: list[datetime] = []

    async def _run_once(
        self,
        prompt: str,
        *,
        evidence_tools: Any,
        deadline: datetime,
    ) -> str:
        self.prompts.append(prompt)
        self.deadlines.append(deadline)
        return next(self.responses)


def _agent(
    responses: tuple[str, ...],
    *,
    clock: Callable[[], datetime] | None = None,
) -> _ResponseAgent:
    return _ResponseAgent(
        responses,
        kind=SpecialistKind.METRICS,
        model_name="gemini-test",
        skill_instruction=METRICS_SKILL.body,
        clock=clock or (lambda: NOW),
    )


@pytest.mark.asyncio
async def test_unknown_citation_gets_exactly_one_safe_correction() -> None:
    tools = _EvidenceTools()
    agent = _agent((_draft(UNKNOWN).model_dump_json(), _draft().model_dump_json()))

    result = await agent.analyze(
        request=_request(),
        evidence_tools=_as_session(tools),
        deadline=NOW + timedelta(minutes=1),
    )

    assert result == _draft()
    assert len(agent.prompts) == 2
    first = json.loads(agent.prompts[0])
    correction = json.loads(agent.prompts[1])
    assert first["alertValues"] == {
        "rawText": "CPU saturation is above the alert threshold.",
        "untrusted": True,
    }
    assert first["specialist"] == "metrics"
    assert first["approvedScope"] == {
        "provider": "GCP",
        "scopeId": "project-a",
        "safe": True,
    }
    assert first["approvedTimeWindow"] == {
        "start": "2026-08-24T07:45:00Z",
        "end": "2026-08-24T08:00:00Z",
    }
    assert first["allowedEvidenceReferenceFormat"] == {
        "id": "UUID",
        "partitionTimestamp": "RFC3339",
    }
    assert first["outputLanguage"] == "zh-TW"
    assert first["constraints"] == {
        "finalRootCauseAllowed": False,
        "remediationAllowed": False,
        "readOnly": True,
    }
    assert correction["validationCorrection"] == "ANALYSIS_UNKNOWN_EVIDENCE"
    assert correction["allowedEvidenceReferences"] == [OWNED.model_dump(mode="json")]
    assert str(UNKNOWN.id) not in agent.prompts[1]
    assert "private_metrics_transport_tool" not in agent.prompts[0]
    assert "private_project_argument" not in agent.prompts[0]
    assert "MCP_TRANSPORT" not in agent.prompts[0]
    assert "raw_result" not in agent.prompts[0]
    assert "http://" not in agent.prompts[0]
    assert "https://" not in agent.prompts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        ('{"specialist":"metrics"}', "ANALYSIS_SCHEMA_INVALID"),
        (_draft(UNKNOWN).model_dump_json(), "ANALYSIS_UNKNOWN_EVIDENCE"),
    ],
)
async def test_second_invalid_response_exposes_only_a_stable_code(
    response: str, expected_code: str
) -> None:
    agent = _agent((response, response))

    with pytest.raises(SpecialistAnalysisValidationError) as raised:
        await agent.analyze(
            request=_request(),
            evidence_tools=_as_session(_EvidenceTools()),
            deadline=NOW + timedelta(minutes=1),
        )

    assert len(agent.prompts) == 2
    assert raised.value.code == expected_code
    assert str(raised.value) == expected_code
    assert str(UNKNOWN.id) not in agent.prompts[1]
    assert response not in agent.prompts[1]


@pytest.mark.asyncio
async def test_truncated_tool_input_limits_complete_analysis_to_partial() -> None:
    agent = _agent((_draft().model_dump_json(),))

    result = await agent.analyze(
        request=_request(),
        evidence_tools=_as_session(_EvidenceTools(truncated=True)),
        deadline=NOW + timedelta(minutes=1),
    )

    assert result.status == "PARTIAL"
    assert result.missing_evidence == ("ANALYSIS_INPUT_TRUNCATED",)


@pytest.mark.asyncio
async def test_deadline_is_rechecked_before_corrective_retry() -> None:
    times = iter((NOW, NOW + timedelta(minutes=1)))
    agent = _agent(('{"specialist":"metrics"}',), clock=lambda: next(times))

    with pytest.raises(TimeoutError, match="ANALYSIS_TIMEOUT"):
        await agent.analyze(
            request=_request(),
            evidence_tools=_as_session(_EvidenceTools()),
            deadline=NOW + timedelta(minutes=1),
        )

    assert len(agent.prompts) == 1


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore:BaseAgentConfig is deprecated and will be removed in future "
    "versions:DeprecationWarning"
)
@pytest.mark.filterwarnings(
    "ignore:\\[EXPERIMENTAL\\] feature "
    "FeatureName.JSON_SCHEMA_FOR_FUNC_DECL is enabled\\.:UserWarning"
)
async def test_real_adk_runner_excludes_thought_text_from_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_model = _DeterministicLlm(
        model="deterministic-fake",
        response=Content(
            role="model",
            parts=[
                Part(text="private chain of thought", thought=True),
                Part(text=_draft().model_dump_json()),
            ],
        ),
    )
    _install_fake_model(monkeypatch, fake_model)
    agent = AdkSpecialistAgent(
        kind=SpecialistKind.METRICS,
        model_name="deterministic-fake",
        skill_instruction=METRICS_SKILL.body,
    )

    result = await agent.analyze(
        request=_request(),
        evidence_tools=_as_session(_EvidenceTools()),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )

    assert result == _draft()
    assert len(fake_model.requests) == 1


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore:BaseAgentConfig is deprecated and will be removed in future "
    "versions:DeprecationWarning"
)
@pytest.mark.filterwarnings(
    "ignore:\\[EXPERIMENTAL\\] feature "
    "FeatureName.JSON_SCHEMA_FOR_FUNC_DECL is enabled\\.:UserWarning"
)
@pytest.mark.parametrize(
    ("kind", "expected_name"),
    [
        (SpecialistKind.METRICS, "metrics_specialist_agent"),
        (SpecialistKind.TRACE, "trace_specialist_agent"),
        (SpecialistKind.LOG, "log_specialist_agent"),
    ],
)
async def test_real_adk_agent_has_exact_specialist_contract(
    monkeypatch: pytest.MonkeyPatch,
    kind: SpecialistKind,
    expected_name: str,
) -> None:
    from google.adk.agents import LlmAgent

    fake_model = _DeterministicLlm(
        model="deterministic-fake",
        response=Content(role="model", parts=[Part(text="unused")]),
    )
    _install_fake_model(monkeypatch, fake_model)
    adapter = AdkSpecialistAgent(
        kind=kind,
        model_name="deterministic-fake",
        skill_instruction=SKILLS[kind].body,
    )

    built = adapter._build_agent(_as_session(_EvidenceTools()))
    canonical_tools = await built.canonical_tools()
    declarations = [tool._get_declaration() for tool in canonical_tools]

    assert isinstance(built, LlmAgent)
    assert built.name == expected_name
    assert built.model == "deterministic-fake"
    assert built.instruction == SKILLS[kind].body
    assert built.output_schema is SpecialistAnalysisDraft
    assert cast(Any, built).mode == "chat"
    assert [declaration.name for declaration in declarations] == [
        "collect_evidence",
        "read_evidence_chunk",
    ]
    assert declarations[0].parameters_json_schema is None
    assert set(declarations[1].parameters_json_schema["properties"]) == {
        "evidence_id",
        "chunk_index",
    }


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore:BaseAgentConfig is deprecated and will be removed in future "
    "versions:DeprecationWarning"
)
async def test_run_once_builds_the_real_adk_agent_contract_and_closes_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.adk.agents
    import google.adk.runners

    captured: dict[str, Any] = {}

    def fake_llm_agent(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    class SessionService:
        async def create_session(self, **kwargs: Any) -> None:
            captured["session"] = kwargs

    class FinalEvent:
        content = Content(role="model", parts=[Part(text=_draft().model_dump_json())])

        @staticmethod
        def is_final_response() -> bool:
            return True

    class Runner:
        def __init__(self, *, agent: object, app_name: str) -> None:
            captured["runner_agent"] = agent
            captured["app_name"] = app_name
            captured["closed"] = False
            self.session_service = SessionService()

        async def run_async(self, **kwargs: Any):
            captured["run"] = kwargs
            yield FinalEvent()

        async def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(google.adk.agents, "LlmAgent", fake_llm_agent)
    monkeypatch.setattr(google.adk.runners, "InMemoryRunner", Runner)
    tools = _EvidenceTools()
    agent = AdkSpecialistAgent(
        kind=SpecialistKind.METRICS,
        model_name="gemini-test",
        skill_instruction=METRICS_SKILL.body,
    )

    response = await agent._run_once(
        '{"safe":"prompt"}',
        evidence_tools=_as_session(tools),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    assert response == _draft().model_dump_json()
    assert captured["name"] == "metrics_specialist_agent"
    assert captured["model"] == "gemini-test"
    assert captured["instruction"] == METRICS_SKILL.body
    assert captured["output_schema"] is SpecialistAnalysisDraft
    assert captured["mode"] == "chat"
    assert [tool.__name__ for tool in captured["tools"]] == [
        "collect_evidence",
        "read_evidence_chunk",
    ]
    assert tuple(inspect.signature(captured["tools"][0]).parameters) == ()
    assert tuple(inspect.signature(captured["tools"][1]).parameters) == (
        "evidence_id",
        "chunk_index",
    )
    assert captured["closed"] is True
    assert captured["run"]["new_message"].parts[0].text == '{"safe":"prompt"}'

    collect_evidence, read_evidence_chunk = captured["tools"]
    receipt = await collect_evidence()
    chunk = await read_evidence_chunk(str(OWNED.id), 0)
    assert receipt["references"] == [OWNED.model_dump(mode="json")]
    assert chunk["reference"] == OWNED.model_dump(mode="json")
    assert tools.read_calls == [(OWNED.id, 0)]
    with pytest.raises(EvidenceToolError) as raised:
        await read_evidence_chunk("not-a-uuid", 0)
    assert raised.value.code == "ANALYSIS_UNKNOWN_EVIDENCE"


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore:BaseAgentConfig is deprecated and will be removed in future "
    "versions:DeprecationWarning"
)
async def test_session_setup_timeout_is_stable_and_never_starts_model_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.adk.agents
    import google.adk.runners

    session_started = False
    run_started = False
    closed = False

    class SessionService:
        async def create_session(self, **kwargs: Any) -> None:
            nonlocal session_started
            session_started = True
            await asyncio.sleep(1)

    class Runner:
        def __init__(self, *, agent: object, app_name: str) -> None:
            self.session_service = SessionService()

        async def run_async(self, **kwargs: Any):
            nonlocal run_started
            run_started = True
            yield None

        async def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(google.adk.agents, "LlmAgent", lambda **kwargs: object())
    monkeypatch.setattr(google.adk.runners, "InMemoryRunner", Runner)
    agent = AdkSpecialistAgent(
        kind=SpecialistKind.METRICS,
        model_name="gemini-test",
        skill_instruction=METRICS_SKILL.body,
    )

    with pytest.raises(TimeoutError) as raised:
        await agent._run_once(
            '{"safe":"prompt"}',
            evidence_tools=_as_session(_EvidenceTools()),
            deadline=datetime.now(UTC) + timedelta(milliseconds=50),
        )

    assert str(raised.value) == "ANALYSIS_TIMEOUT"
    assert session_started is True
    assert run_started is False
    assert closed is True


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore:BaseAgentConfig is deprecated and will be removed in future "
    "versions:DeprecationWarning"
)
async def test_run_once_closes_runner_when_adk_execution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.adk.agents
    import google.adk.runners

    closed = False

    class SessionService:
        async def create_session(self, **kwargs: Any) -> None:
            pass

    class Runner:
        def __init__(self, *, agent: object, app_name: str) -> None:
            self.session_service = SessionService()

        async def run_async(self, **kwargs: Any):
            if False:
                yield None
            raise RuntimeError("upstream execution failed")

        async def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(google.adk.agents, "LlmAgent", lambda **kwargs: object())
    monkeypatch.setattr(google.adk.runners, "InMemoryRunner", Runner)
    agent = AdkSpecialistAgent(
        kind=SpecialistKind.METRICS,
        model_name="gemini-test",
        skill_instruction=METRICS_SKILL.body,
    )

    with pytest.raises(RuntimeError, match="upstream execution failed"):
        await agent._run_once(
            '{"safe":"prompt"}',
            evidence_tools=_as_session(_EvidenceTools()),
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        )

    assert closed is True
