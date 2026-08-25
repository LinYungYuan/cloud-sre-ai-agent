from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sre_rca_worker.agents.rca.adk_agent import AdkRcaAgent
from sre_rca_worker.agents.rca.models import (
    IncidentContext,
    InvestigationBundle,
    SpecialistAnalysisBundle,
    SpecialistAnalysisResult,
    SpecialistFailure,
)
from sre_rca_worker.agents.rca.synthesizer import RcaSynthesizer
from sre_rca_worker.agents.rca.workflow import RcaWorkflow
from sre_rca_worker.agents.skills.loader import load_skills
from sre_rca_worker.agents.skills.registry import SkillRegistry
from sre_rca_worker.agents.specialists.adk_agent import AdkSpecialistAgent
from sre_rca_worker.agents.specialists.base import SpecialistRequest
from sre_rca_worker.agents.specialists.log_agent import LogSpecialist
from sre_rca_worker.agents.specialists.metrics_agent import MetricsSpecialist
from sre_rca_worker.agents.specialists.trace_agent import TraceSpecialist
from sre_rca_worker.agents.specialists.workflow import (
    SpecialistAnalysisWorkflow,
)
from sre_rca_worker.application.rca.evidence_tools import EvidenceToolSession
from sre_rca_worker.application.rca.job_lifecycle import (
    RcaJobClaim,
    RcaProcessingResult,
)
from sre_rca_worker.application.rca.persist_evidence import PersistEvidence
from sre_rca_worker.config.settings import SpecialistAnalysisMode, WorkerSettings
from sre_rca_worker.domain.evidence.analysis import StableSpecialistCode
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.domain.rca.models import EvidenceClaim, RcaReportDraft
from sre_rca_worker.integrations.mcp.client import McpClient
from sre_rca_worker.integrations.mcp.discovery import discover_capabilities
from sre_rca_worker.integrations.mcp.factories import McpClientFactory
from sre_rca_worker.integrations.mcp.models import (
    CapabilitySet,
    CloudScope,
    SpecialistKind,
)
from sre_rca_worker.persistence.repositories.rca import (
    RcaRepository,
    SpecialistAnalysisOwnershipError,
)

_SPECIALIST_ORDER = (
    SpecialistKind.METRICS,
    SpecialistKind.TRACE,
    SpecialistKind.LOG,
)
_SPECIALIST_TYPES = {
    SpecialistKind.METRICS: "METRICS",
    SpecialistKind.TRACE: "TRACES",
    SpecialistKind.LOG: "LOGS",
}
_STABLE_SPECIALIST_CODES = frozenset(get_args(StableSpecialistCode))


class ProductionRcaProcessor:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: WorkerSettings,
        *,
        root_agent_factory: Callable[..., Any] | None = None,
        specialist_agent_factory: Callable[..., Any] | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        definitions = Path(__file__).parents[2] / "agents/skills/definitions"
        self._skills = skill_registry or SkillRegistry(load_skills(definitions))
        self._root_agent_factory = root_agent_factory or AdkRcaAgent
        self._specialist_agent_factory = specialist_agent_factory or AdkSpecialistAgent
        self._analysis_workflow_factory = SpecialistAnalysisWorkflow

    async def __call__(self, claim: RcaJobClaim) -> RcaProcessingResult:
        context = await self._load_context(claim)
        if (
            context.scope is None
            or context.scope.provider != "GCP"
            or not context.scope.safe
        ):
            report = RcaSynthesizer().insufficient_evidence(
                provider=context.scope.provider if context.scope else None
            )
            await self._persist_report(claim, report)
            return RcaProcessingResult(status=report.status)
        factory = McpClientFactory(self._settings)
        capabilities, clients = await discover_capabilities(
            factory,
            context.scope,
            self._settings.mcp_capability_manifest,
            self._skills.required_capabilities(),
        )
        mode = getattr(
            self._settings,
            "specialist_analysis_mode",
            SpecialistAnalysisMode.DISABLED,
        )
        if mode is SpecialistAnalysisMode.DISABLED:
            report = await self._run_legacy(
                claim,
                context,
                capabilities,
                clients,
            )
        else:
            report = await self._run_specialist_analysis(
                claim,
                context,
                capabilities,
                clients,
                mode=mode,
            )
        await self._persist_report(claim, report)
        return RcaProcessingResult(status=report.status)

    def _legacy_specialists(
        self,
        clients: Mapping[SpecialistKind, McpClient],
    ) -> dict[SpecialistKind, MetricsSpecialist | TraceSpecialist | LogSpecialist]:
        return {
            SpecialistKind.METRICS: MetricsSpecialist(
                lambda: clients[SpecialistKind.METRICS],
                max_response_bytes=self._settings.mcp_max_response_bytes,
            ),
            SpecialistKind.TRACE: TraceSpecialist(
                lambda: clients[SpecialistKind.TRACE],
                max_response_bytes=self._settings.mcp_max_response_bytes,
            ),
            SpecialistKind.LOG: LogSpecialist(
                lambda: clients[SpecialistKind.LOG],
                max_response_bytes=self._settings.mcp_max_response_bytes,
            ),
        }

    async def _run_legacy(
        self,
        claim: RcaJobClaim,
        context: IncidentContext,
        capabilities: CapabilitySet,
        clients: Mapping[SpecialistKind, McpClient],
    ) -> RcaReportDraft:
        bundle = await RcaWorkflow(self._legacy_specialists(clients)).run(
            context, capabilities, deadline=claim.deadline_at
        )
        await self._persist_failures(claim, bundle.failures)
        self._raise_if_retryable_total_failure(bundle, claim.attempt_number)
        references, summaries = await self._persist_bundle(claim, bundle.results)
        if not references:
            report = (
                RcaSynthesizer().failed_analysis()
                if bundle.failures
                else RcaSynthesizer().insufficient_evidence(
                    provider=context.scope.provider if context.scope else None
                )
            )
        else:
            report = await self._root_agent().synthesize_legacy(
                alert_issue=context.alert_issue,
                evidence_summaries=summaries,
                known_evidence=references,
                deadline=claim.deadline_at,
            )
            if bundle.failures:
                report = RcaSynthesizer().with_specialist_failures(report)
        return report

    async def _run_specialist_analysis(
        self,
        claim: RcaJobClaim,
        context: IncidentContext,
        capabilities: CapabilitySet,
        clients: Mapping[SpecialistKind, McpClient],
        *,
        mode: SpecialistAnalysisMode,
    ) -> RcaReportDraft:
        async def invoke(
            request: SpecialistRequest,
            kind: SpecialistKind,
            deadline: datetime,
        ) -> SpecialistAnalysisResult:
            return await self._invoke_specialist_branch(
                request,
                kind,
                deadline,
                clients=clients,
            )

        workflow_factory = getattr(
            self,
            "_analysis_workflow_factory",
            SpecialistAnalysisWorkflow,
        )
        bundle = await workflow_factory(invoke).run(
            context,
            capabilities,
            deadline=claim.deadline_at,
        )
        bundle = self._ordered_analysis_bundle(bundle)
        persisted_results, audit_failures = await self._persist_specialist_analyses(
            claim,
            bundle.results,
        )
        bundle = self._ordered_analysis_bundle(
            SpecialistAnalysisBundle(
                results=persisted_results,
                failures=(*bundle.failures, *audit_failures),
            )
        )
        await self._persist_analysis_failures(claim, bundle.failures)
        self._raise_if_retryable_analysis_total_failure(bundle, claim.attempt_number)

        usable = tuple(
            result for result in bundle.results if result.analysis.observations
        )
        if not usable:
            if bundle.failures or bundle.results:
                return RcaSynthesizer().failed_analysis()
            return RcaSynthesizer().insufficient_evidence(
                provider=context.scope.provider if context.scope else None
            )

        references = self._known_evidence(bundle.results)
        if mode is SpecialistAnalysisMode.SHADOW:
            report = await self._root_agent().synthesize_legacy(
                alert_issue=context.alert_issue,
                evidence_summaries=self._legacy_summaries(bundle.results),
                known_evidence=references,
                deadline=claim.deadline_at,
            )
        else:
            report = await self._root_agent().synthesize(
                alert_issue=context.alert_issue,
                specialist_analyses=tuple(result.analysis for result in bundle.results),
                known_evidence=references,
                deadline=claim.deadline_at,
            )
        if bundle.failures or any(
            result.analysis.status != "COMPLETE" for result in bundle.results
        ):
            report = RcaSynthesizer().with_specialist_failures(report)
        return report

    def _root_agent(self) -> Any:
        factory = getattr(self, "_root_agent_factory", AdkRcaAgent)
        return factory(
            model_name=self._settings.model_name,
            skill_instruction=self._skills.get_for_agent("rca").body,
        )

    @staticmethod
    def _ordered_analysis_bundle(
        bundle: SpecialistAnalysisBundle,
    ) -> SpecialistAnalysisBundle:
        results = {result.analysis.specialist: result for result in bundle.results}
        failures = {failure.specialist: failure for failure in bundle.failures}
        return SpecialistAnalysisBundle(
            results=tuple(
                results[kind] for kind in _SPECIALIST_ORDER if kind in results
            ),
            failures=tuple(
                failures[kind] for kind in _SPECIALIST_ORDER if kind in failures
            ),
        )

    @staticmethod
    def _known_evidence(
        results: tuple[SpecialistAnalysisResult, ...],
    ) -> tuple[EvidenceReference, ...]:
        references: list[EvidenceReference] = []
        seen: set[tuple[UUID, datetime]] = set()
        for result in results:
            for reference in result.known_evidence:
                key = (reference.id, reference.partition_timestamp)
                if key not in seen:
                    seen.add(key)
                    references.append(reference)
        return tuple(references)

    @staticmethod
    def _legacy_summaries(
        results: tuple[SpecialistAnalysisResult, ...],
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "specialist": result.analysis.specialist.value,
                "summary": (f"{result.analysis.specialist.value} MCP 回傳可用觀測資料"),
                "confidence": 0.5,
                "evidenceReference": reference.model_dump(mode="json"),
            }
            for result in results
            for reference in result.known_evidence
        )

    async def _invoke_specialist_branch(
        self,
        request: SpecialistRequest,
        kind: SpecialistKind,
        deadline: datetime,
        *,
        clients: Mapping[SpecialistKind, McpClient],
    ) -> SpecialistAnalysisResult:
        specialist_run_id = await self._get_or_create_specialist_run(
            request.rca_run_id,
            kind,
        )
        async with self._specialist_collection_reservation(
            request.rca_run_id,
            kind,
            deadline,
        ):
            collector = self._legacy_specialists(clients)[kind]
            evidence_tools = EvidenceToolSession(
                request=request,
                specialist_run_id=specialist_run_id,
                collector=collector,
                sessions=self._sessions,
                deadline=deadline,
                chunk_chars=self._settings.evidence_chunk_chars,
                max_chunks=self._settings.evidence_max_chunks,
                max_total_chars=self._settings.evidence_max_total_chars,
                max_tool_calls=self._settings.specialist_max_tool_calls,
            )
            skill = self._skills.get_for_agent(kind.value)
            agent_factory = getattr(
                self,
                "_specialist_agent_factory",
                AdkSpecialistAgent,
            )
            agent = agent_factory(
                kind=kind,
                model_name=self._settings.model_name,
                skill_instruction=skill.body,
            )
            analysis = await agent.analyze(
                request=request,
                evidence_tools=evidence_tools,
                deadline=deadline,
            )
            return SpecialistAnalysisResult(
                analysis=analysis,
                known_evidence=evidence_tools.known_evidence,
            )

    @asynccontextmanager
    async def _specialist_collection_reservation(
        self,
        rca_run_id: UUID,
        kind: SpecialistKind,
        deadline: datetime,
    ):
        """Serialize one run/kind from evidence reuse through agent analysis."""
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError
        lock_key = self._collection_reservation_key(rca_run_id, kind)
        async with self._sessions() as session:
            async with asyncio.timeout(remaining):
                async with session.begin():
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:lock_key)"),
                        {"lock_key": lock_key},
                    )
                    yield

    @staticmethod
    def _collection_reservation_key(rca_run_id: UUID, kind: SpecialistKind) -> int:
        digest = hashlib.sha256(f"{rca_run_id}:{kind.value}".encode("ascii")).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)

    async def _get_or_create_specialist_run(
        self,
        rca_run_id: UUID,
        kind: SpecialistKind,
    ) -> UUID:
        async with self._sessions() as session, session.begin():
            specialist_run_id = await session.scalar(
                text(
                    """INSERT INTO specialist_runs(
                           rca_run_id,specialist_type,status,started_at)
                       VALUES (:run,:kind,'RUNNING',now())
                       ON CONFLICT (rca_run_id,specialist_type) DO UPDATE
                       SET status='RUNNING', completed_at=NULL, failure_code=NULL,
                           started_at=COALESCE(specialist_runs.started_at,now())
                       RETURNING id"""
                ),
                {"run": rca_run_id, "kind": _SPECIALIST_TYPES[kind]},
            )
        if not isinstance(specialist_run_id, UUID):
            raise TypeError("persisted specialist run id must be a UUID")
        return specialist_run_id

    async def _persist_specialist_analyses(
        self,
        claim: RcaJobClaim,
        results: tuple[SpecialistAnalysisResult, ...],
    ) -> tuple[tuple[SpecialistAnalysisResult, ...], tuple[SpecialistFailure, ...]]:
        persisted: list[SpecialistAnalysisResult] = []
        failures: list[SpecialistFailure] = []
        for result in results:
            skill = self._skills.get_for_agent(result.analysis.specialist.value)
            try:
                async with self._sessions() as session, session.begin():
                    await RcaRepository(session).upsert_specialist_analysis(
                        rca_run_id=claim.rca_run_id,
                        specialist=result.analysis.specialist,
                        analysis=result.analysis,
                        model_name=self._settings.model_name,
                        skill_name=skill.name,
                        skill_sha256=hashlib.sha256(
                            skill.body.encode("utf-8")
                        ).hexdigest(),
                    )
            except SpecialistAnalysisOwnershipError:
                failures.append(
                    SpecialistFailure(
                        specialist=result.analysis.specialist,
                        code="ANALYSIS_UNKNOWN_EVIDENCE",
                    )
                )
            else:
                persisted.append(result)
        return tuple(persisted), tuple(failures)

    async def _persist_analysis_failures(
        self,
        claim: RcaJobClaim,
        failures: tuple[SpecialistFailure, ...],
    ) -> None:
        async with self._sessions() as session, session.begin():
            for failure in failures:
                if failure.code not in _STABLE_SPECIALIST_CODES:
                    raise ValueError("analysis failure requires a stable code")
                await session.execute(
                    text(
                        """INSERT INTO specialist_runs(
                               rca_run_id,specialist_type,status,started_at,
                               completed_at,failure_code)
                           VALUES (:run,:kind,'FAILED',now(),now(),:failure_code)
                           ON CONFLICT (rca_run_id,specialist_type) DO UPDATE
                           SET status='FAILED', completed_at=now(),
                               failure_code=EXCLUDED.failure_code,
                               analysis_result=NULL, model_name=NULL,
                               skill_name=NULL, skill_sha256=NULL, analyzed_at=NULL"""
                    ),
                    {
                        "run": claim.rca_run_id,
                        "kind": _SPECIALIST_TYPES[failure.specialist],
                        "failure_code": failure.code,
                    },
                )

    @staticmethod
    def _raise_if_retryable_analysis_total_failure(
        bundle: SpecialistAnalysisBundle,
        attempt_number: int,
    ) -> None:
        if (
            attempt_number < 3
            and not bundle.results
            and bundle.failures
            and all(failure.code == "MCP_TRANSPORT" for failure in bundle.failures)
        ):
            raise ConnectionError("transient MCP failure")

    async def _load_context(self, claim: RcaJobClaim) -> IncidentContext:
        async with self._sessions() as session:
            row = (
                (
                    await session.execute(
                        text(
                            """SELECT event.provider, event.issue, event.resource,
                                  COALESCE(event.starts_at, event.observed_at - interval '15 minutes') AS window_start,
                                  event.observed_at AS window_end
                           FROM rca_runs run
                           JOIN incident_alerts link ON link.incident_id=run.incident_id
                           JOIN alert_events event ON event.id=link.alert_event_id
                             AND event.partition_timestamp=link.alert_event_partition_timestamp
                           WHERE run.id=:run_id AND run.incident_id=:incident_id
                           ORDER BY event.observed_at DESC LIMIT 1"""
                        ),
                        {"run_id": claim.rca_run_id, "incident_id": claim.incident_id},
                    )
                )
                .mappings()
                .one()
            )
        resource = row["resource"] if isinstance(row["resource"], dict) else None
        scope_id = resource.get("scopeId") if resource else None
        provider = row["provider"]
        scope = (
            CloudScope(provider=provider, scope_id=scope_id, safe=True)
            if provider in {"GCP", "AWS"}
            and isinstance(scope_id, str)
            and scope_id.strip()
            else (
                CloudScope(provider="AWS", scope_id="unclassified", safe=False)
                if provider == "AWS"
                else None
            )
        )
        issue = row["issue"] if isinstance(row["issue"], dict) else {}
        return IncidentContext(
            incident_id=claim.incident_id,
            rca_run_id=claim.rca_run_id,
            alert_issue=str(issue.get("rawText") or "AlertValues 未提供"),
            scope=scope,
            window_start=row["window_start"],
            window_end=row["window_end"],
        )

    async def _persist_bundle(self, claim, results):  # type: ignore[no-untyped-def]
        references: list[EvidenceReference] = []
        summaries: list[dict[str, object]] = []
        async with self._sessions() as session, session.begin():
            for result in results:
                specialist_id = await session.scalar(
                    text(
                        """INSERT INTO specialist_runs(rca_run_id,specialist_type,status,started_at,completed_at)
                           VALUES (:run,:kind,'SUCCEEDED',now(),now())
                           ON CONFLICT (rca_run_id,specialist_type) DO UPDATE
                           SET status='SUCCEEDED', completed_at=now(), failure_code=NULL
                           RETURNING id"""
                    ),
                    {
                        "run": claim.rca_run_id,
                        "kind": {
                            "metrics": "METRICS",
                            "trace": "TRACES",
                            "log": "LOGS",
                        }[result.specialist.value],
                    },
                )
                for finding in result.findings:
                    for draft in finding.evidence:
                        reference = await PersistEvidence(session).save(
                            claim.rca_run_id, specialist_id, draft
                        )
                        references.append(reference)
                        summaries.append(
                            {
                                "specialist": result.specialist.value,
                                "summary": finding.summary,
                                "confidence": finding.confidence,
                                "evidenceReference": reference.model_dump(mode="json"),
                            }
                        )
        return tuple(references), tuple(summaries)

    async def _persist_failures(
        self,
        claim: RcaJobClaim,
        failures: tuple[SpecialistFailure, ...],
    ) -> None:
        specialist_types = {
            SpecialistKind.METRICS: "METRICS",
            SpecialistKind.TRACE: "TRACES",
            SpecialistKind.LOG: "LOGS",
        }
        failure_codes = {
            "SPECIALIST_TIMEOUT": "MCP_TIMEOUT",
            "SPECIALIST_TRANSPORT": "MCP_TRANSPORT",
            "SPECIALIST_VALIDATION": "VALIDATION_FAILED",
            "SPECIALIST_FAILED": "INTERNAL_ERROR",
        }
        async with self._sessions() as session, session.begin():
            for failure in failures:
                await session.execute(
                    text(
                        """INSERT INTO specialist_runs(
                              rca_run_id,specialist_type,status,started_at,
                              completed_at,failure_code)
                           VALUES (:run,:kind,'FAILED',now(),now(),:failure_code)
                           ON CONFLICT (rca_run_id,specialist_type) DO UPDATE
                           SET status='FAILED', completed_at=now(),
                               failure_code=EXCLUDED.failure_code"""
                    ),
                    {
                        "run": claim.rca_run_id,
                        "kind": specialist_types[failure.specialist],
                        "failure_code": failure_codes[failure.code],
                    },
                )

    @staticmethod
    def _raise_if_retryable_total_failure(
        bundle: InvestigationBundle, attempt_number: int
    ) -> None:
        if (
            attempt_number < 3
            and not bundle.results
            and bundle.failures
            and all(
                failure.code == "SPECIALIST_TRANSPORT" for failure in bundle.failures
            )
        ):
            raise ConnectionError("transient MCP failure")

    async def _persist_report(self, claim: RcaJobClaim, report: RcaReportDraft) -> None:
        def serialize_claim(claim_item: EvidenceClaim) -> dict[str, Any]:
            return {
                "statement": claim_item.statement,
                "evidence": [
                    {
                        "evidenceId": str(reference.id),
                        "partitionTimestamp": reference.partition_timestamp.isoformat().replace(
                            "+00:00", "Z"
                        ),
                        "relation": claim_item.relation,
                    }
                    for reference in claim_item.evidence
                ],
            }

        body: dict[str, Any] = {
            "status": report.status,
            "rootCause": (
                report.hypotheses[0].statement if report.hypotheses else "尚待確認"
            ),
            "confidence": (
                report.hypotheses[0].confidence if report.hypotheses else None
            ),
            "impact": "；".join(
                claim.statement
                for hypothesis in report.hypotheses
                for claim in hypothesis.claims
            )
            or "證據不足",
            "recommendations": list(report.remediation),
            "hypotheses": [
                {
                    "statement": item.statement,
                    "confidence": item.confidence,
                    "claims": [serialize_claim(claim) for claim in item.claims],
                }
                for item in report.hypotheses
            ],
            "claims": [
                serialize_claim(claim)
                for hypothesis in report.hypotheses
                for claim in hypothesis.claims
            ],
            "verificationSteps": list(report.verification_steps),
            "missingEvidence": list(report.missing_evidence),
        }
        async with self._sessions() as session, session.begin():
            for hypothesis in report.hypotheses:
                hypothesis_id = await session.scalar(
                    text(
                        """INSERT INTO rca_hypotheses(rca_run_id,statement,confidence)
                           VALUES (:run,:statement,:confidence) RETURNING id"""
                    ),
                    {
                        "run": claim.rca_run_id,
                        "statement": hypothesis.statement,
                        "confidence": hypothesis.confidence,
                    },
                )
                for claim_item in hypothesis.claims:
                    for reference in claim_item.evidence:
                        await session.execute(
                            text(
                                """INSERT INTO hypothesis_evidence(
                                      hypothesis_id,evidence_id,
                                      evidence_partition_timestamp,relation)
                                   VALUES (:hypothesis,:evidence,:partition,:relation)
                                   ON CONFLICT DO NOTHING"""
                            ),
                            {
                                "hypothesis": hypothesis_id,
                                "evidence": reference.id,
                                "partition": reference.partition_timestamp,
                                "relation": claim_item.relation,
                            },
                        )
            await session.execute(
                text(
                    """INSERT INTO rca_reports(rca_run_id,version,summary,report,result_status)
                       SELECT :run, COALESCE(max(version),0)+1, :summary,
                              CAST(:report AS JSONB), :status
                       FROM rca_reports WHERE rca_run_id=:run"""
                ),
                {
                    "run": claim.rca_run_id,
                    "summary": report.summary_zh_tw,
                    "report": json.dumps(body, ensure_ascii=False),
                    "status": report.status,
                },
            )
