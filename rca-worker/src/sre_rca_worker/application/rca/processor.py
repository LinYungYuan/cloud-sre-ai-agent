from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sre_rca_worker.agents.rca.adk_agent import AdkRcaAgent
from sre_rca_worker.agents.rca.models import (
    IncidentContext,
    InvestigationBundle,
    SpecialistFailure,
)
from sre_rca_worker.agents.rca.synthesizer import RcaSynthesizer
from sre_rca_worker.agents.rca.workflow import RcaWorkflow
from sre_rca_worker.agents.skills.loader import load_skills
from sre_rca_worker.agents.skills.registry import SkillRegistry
from sre_rca_worker.agents.specialists.log_agent import LogSpecialist
from sre_rca_worker.agents.specialists.metrics_agent import MetricsSpecialist
from sre_rca_worker.agents.specialists.trace_agent import TraceSpecialist
from sre_rca_worker.application.rca.job_lifecycle import (
    RcaJobClaim,
    RcaProcessingResult,
)
from sre_rca_worker.application.rca.persist_evidence import PersistEvidence
from sre_rca_worker.config.settings import WorkerSettings
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.domain.rca.models import EvidenceClaim, RcaReportDraft
from sre_rca_worker.integrations.mcp.discovery import discover_capabilities
from sre_rca_worker.integrations.mcp.factories import McpClientFactory
from sre_rca_worker.integrations.mcp.models import CloudScope, SpecialistKind


class ProductionRcaProcessor:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: WorkerSettings,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        definitions = Path(__file__).parents[2] / "agents/skills/definitions"
        self._skills = SkillRegistry(load_skills(definitions))

    async def __call__(self, claim: RcaJobClaim) -> RcaProcessingResult:
        context = await self._load_context(claim)
        factory = McpClientFactory(self._settings)
        capabilities, clients = await discover_capabilities(
            factory,
            context.scope,
            self._settings.mcp_capability_manifest,
            self._skills.required_capabilities(),
        )
        specialists = {
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
        bundle = await RcaWorkflow(specialists).run(
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
            report = await AdkRcaAgent(
                model_name=self._settings.model_name,
                skill_instruction=self._skills.get_for_agent("rca").body,
            ).synthesize_legacy(
                alert_issue=context.alert_issue,
                evidence_summaries=summaries,
                known_evidence=references,
                deadline=claim.deadline_at,
            )
            if bundle.failures:
                report = RcaSynthesizer().with_specialist_failures(report)
        await self._persist_report(claim, report)
        return RcaProcessingResult(status=report.status)

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
