from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from sre_rca_worker.agents.specialists.base import SpecialistRequest
from sre_rca_worker.agents.specialists.validator import (
    SpecialistAnalysisValidationError,
    SpecialistAnalysisValidator,
    SpecialistValidationCode,
)
from sre_rca_worker.application.rca.evidence_tools import (
    EvidenceToolError,
    EvidenceToolSession,
)
from sre_rca_worker.domain.evidence.analysis import SpecialistAnalysisDraft
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.integrations.mcp.models import SpecialistKind

_APP_NAME = "sre_rca_worker"


class AdkSpecialistAgent:
    """Run one read-only ADK specialist over persisted, bounded evidence."""

    def __init__(
        self,
        *,
        kind: SpecialistKind,
        model_name: str,
        skill_instruction: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(kind, SpecialistKind):
            raise TypeError("kind must be a SpecialistKind")
        if not model_name:
            raise ValueError("model_name must not be empty")
        if not skill_instruction:
            raise ValueError("skill_instruction must not be empty")
        self.kind = kind
        self._model_name = model_name
        self._instruction = skill_instruction
        self._clock = clock or (lambda: datetime.now(UTC))
        self._validator = SpecialistAnalysisValidator()

    async def analyze(
        self,
        *,
        request: SpecialistRequest,
        evidence_tools: EvidenceToolSession,
        deadline: datetime,
    ) -> SpecialistAnalysisDraft:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")
        approved_context = self._approved_context(
            request=request,
            allowed_evidence=evidence_tools.known_evidence,
        )
        prompt = self._encode_prompt(approved_context)
        for attempt in range(2):
            self._remaining_seconds(deadline)
            final_text = await self._run_once(
                prompt,
                evidence_tools=evidence_tools,
                deadline=deadline,
            )
            try:
                draft = SpecialistAnalysisDraft.model_validate_json(final_text)
            except (TypeError, ValidationError, ValueError):
                validation_error = SpecialistAnalysisValidationError(
                    "ANALYSIS_SCHEMA_INVALID"
                )
            else:
                try:
                    return self._validator.validate(
                        draft,
                        expected_specialist=self.kind,
                        owned_evidence=evidence_tools.known_evidence,
                        input_truncated=evidence_tools.input_truncated,
                    )
                except SpecialistAnalysisValidationError as error:
                    validation_error = error

            if attempt == 1:
                raise validation_error
            prompt = self._correction_prompt(
                approved_context,
                code=validation_error.code,
                allowed_evidence=evidence_tools.known_evidence,
            )
        raise AssertionError("unreachable")

    async def _run_once(
        self,
        prompt: str,
        *,
        evidence_tools: EvidenceToolSession,
        deadline: datetime,
    ) -> str:
        from google.adk.runners import InMemoryRunner
        from google.genai.types import Content, Part

        agent = self._build_agent(evidence_tools)
        runner = InMemoryRunner(agent=agent, app_name=_APP_NAME)
        user_id = "rca-worker"
        session_id = uuid4().hex
        final_text: str | None = None
        try:
            try:
                async with asyncio.timeout(self._remaining_seconds(deadline)):
                    await runner.session_service.create_session(
                        app_name=_APP_NAME,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    async for event in runner.run_async(
                        user_id=user_id,
                        session_id=session_id,
                        new_message=Content(
                            role="user",
                            parts=[Part(text=prompt)],
                        ),
                    ):
                        if event.is_final_response() and event.content:
                            final_text = "".join(
                                part.text
                                for part in event.content.parts or []
                                if part.text and not part.thought
                            )
            except TimeoutError:
                raise TimeoutError("ANALYSIS_TIMEOUT") from None
        finally:
            await runner.close()
        return final_text or ""

    def _build_tools(
        self, evidence_tools: EvidenceToolSession
    ) -> tuple[Callable[..., Awaitable[dict[str, object]]], ...]:
        async def collect_evidence() -> dict[str, object]:
            """Collect persisted evidence for this approved specialist run."""

            receipt = await evidence_tools.collect_evidence()
            return receipt.model_dump(mode="json")

        async def read_evidence_chunk(
            evidence_id: str, chunk_index: int
        ) -> dict[str, object]:
            """Read one bounded chunk by opaque evidence ID and chunk index."""

            try:
                parsed_id = UUID(evidence_id)
            except (AttributeError, TypeError, ValueError):
                raise EvidenceToolError("ANALYSIS_UNKNOWN_EVIDENCE") from None
            chunk = await evidence_tools.read_evidence_chunk(parsed_id, chunk_index)
            return chunk.model_dump(mode="json")

        return collect_evidence, read_evidence_chunk

    def _build_agent(self, evidence_tools: EvidenceToolSession) -> Any:
        from google.adk.agents import LlmAgent

        return LlmAgent(
            name=f"{self.kind.value}_specialist_agent",
            model=self._model_name,
            instruction=self._instruction,
            output_schema=SpecialistAnalysisDraft,
            # ADK 2.7 exposes mode at runtime but omits it from Pyright's
            # generated Pydantic constructor signature.
            mode="chat",  # pyright: ignore[reportCallIssue]
            tools=list(self._build_tools(evidence_tools)),
        )

    def _remaining_seconds(self, deadline: datetime) -> float:
        remaining = (deadline - self._clock()).total_seconds()
        if remaining <= 0:
            raise TimeoutError("ANALYSIS_TIMEOUT")
        return remaining

    def _approved_context(
        self,
        *,
        request: SpecialistRequest,
        allowed_evidence: tuple[EvidenceReference, ...],
    ) -> dict[str, object]:
        scope = request.scope
        return {
            "specialist": self.kind.value,
            "alertValues": {
                "rawText": request.alert_issue,
                "untrusted": True,
            },
            "approvedScope": (
                None
                if scope is None
                else {
                    "provider": scope.provider,
                    "scopeId": scope.scope_id,
                    "safe": scope.safe,
                }
            ),
            "approvedTimeWindow": {
                "start": self._rfc3339(request.window_start),
                "end": self._rfc3339(request.window_end),
            },
            "allowedEvidenceReferenceFormat": {
                "id": "UUID",
                "partitionTimestamp": "RFC3339",
            },
            "allowedEvidenceReferences": [
                reference.model_dump(mode="json") for reference in allowed_evidence
            ],
            "outputLanguage": "zh-TW",
            "constraints": {
                "finalRootCauseAllowed": False,
                "remediationAllowed": False,
                "readOnly": True,
            },
        }

    def _correction_prompt(
        self,
        approved_context: dict[str, object],
        *,
        code: SpecialistValidationCode,
        allowed_evidence: tuple[EvidenceReference, ...],
    ) -> str:
        correction = {
            **approved_context,
            "allowedEvidenceReferences": [
                reference.model_dump(mode="json") for reference in allowed_evidence
            ],
            "validationCorrection": code,
        }
        return self._encode_prompt(correction)

    @staticmethod
    def _encode_prompt(payload: dict[str, object]) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _rfc3339(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")


__all__ = ["AdkSpecialistAgent"]
