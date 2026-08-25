import json
import os
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sre_rca_worker.agents.rca.models import SpecialistFailure
from sre_rca_worker.application.rca.job_lifecycle import RcaJobClaim
from sre_rca_worker.application.rca.processor import ProductionRcaProcessor
from sre_rca_worker.config.settings import WorkerSettings
from sre_rca_worker.domain.evidence.analysis import (
    SpecialistAnalysisDraft,
    SpecialistObservation,
)
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.domain.rca.models import (
    EvidenceClaim,
    RcaHypothesis,
    RcaReportDraft,
)
from sre_rca_worker.integrations.mcp.models import SpecialistKind
from sre_rca_worker.persistence.repositories.rca import (
    RcaRepository,
    SpecialistAnalysisOwnershipError,
)

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
)


async def _seed_analysis_owner(
    sessions: async_sessionmaker[AsyncSession],
    *,
    specialist_type: str = "METRICS",
) -> tuple[UUID, UUID, EvidenceReference]:
    now = datetime.now(UTC)
    team, project, environment, incident, run, specialist, evidence = [
        uuid4() for _ in range(7)
    ]
    async with sessions() as session, session.begin():
        await session.execute(
            text("INSERT INTO teams(id,name) VALUES (:id,:name)"),
            {"id": team, "name": f"team-{team}"},
        )
        await session.execute(
            text("INSERT INTO projects(id,team_id,name) VALUES (:id,:team,:name)"),
            {"id": project, "team": team, "name": f"project-{project}"},
        )
        await session.execute(
            text(
                "INSERT INTO environments(id,project_id,name) "
                "VALUES (:id,:project,:name)"
            ),
            {"id": environment, "project": project, "name": f"env-{environment}"},
        )
        await session.execute(
            text(
                """INSERT INTO incidents(
                       id,identity_key,title,severity,status,alert_state,
                       team_id,project_id,environment_id,opened_at)
                   VALUES (:id,:identity,'test','SEV3','OPEN','FIRING',
                           :team,:project,:environment,:now)"""
            ),
            {
                "id": incident,
                "identity": f"identity-{incident}",
                "team": team,
                "project": project,
                "environment": environment,
                "now": now,
            },
        )
        await session.execute(
            text(
                "INSERT INTO rca_runs(id,incident_id,status) "
                "VALUES (:id,:incident,'RUNNING')"
            ),
            {"id": run, "incident": incident},
        )
        await session.execute(
            text(
                """INSERT INTO specialist_runs(
                       id,rca_run_id,specialist_type,status,started_at)
                   VALUES (:id,:run,:kind,'RUNNING',:now)"""
            ),
            {"id": specialist, "run": run, "kind": specialist_type, "now": now},
        )
        endpoint = {
            "METRICS": "metrics",
            "TRACES": "trace",
            "LOGS": "log",
        }[specialist_type]
        await session.execute(
            text(
                """INSERT INTO evidence_records(
                       id,partition_timestamp,observed_at,rca_run_id,specialist_run_id,
                       evidence_type,source_agent,source_endpoint,tool_name,
                       time_window_start,time_window_end,structured_data,content_hash,
                       raw_result,metadata)
                   VALUES (:id,:now,:now,:run,:specialist,:evidence_type,:source_agent,
                           :endpoint,:tool,:now,:now,CAST(:structured AS JSONB),'hash',
                           CAST(:raw AS BYTEA),'{}')"""
            ),
            {
                "id": evidence,
                "now": now,
                "run": run,
                "specialist": specialist,
                "evidence_type": f"{endpoint}.query",
                "source_agent": endpoint.upper(),
                "endpoint": endpoint,
                "tool": f"{endpoint}_query",
                "structured": json.dumps({"unsafe": "raw-fixture-secret"}),
                "raw": b"raw-fixture-secret",
            },
        )
    return run, specialist, EvidenceReference(id=evidence, partition_timestamp=now)


async def _seed_additional_specialist_evidence(
    sessions: async_sessionmaker[AsyncSession],
    *,
    run_id: UUID,
    specialist_type: str,
) -> EvidenceReference:
    now = datetime.now(UTC)
    specialist_id, evidence_id = uuid4(), uuid4()
    endpoint = {"TRACES": "trace", "LOGS": "log"}[specialist_type]
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                """INSERT INTO specialist_runs(
                       id,rca_run_id,specialist_type,status,started_at)
                   VALUES (:id,:run,:kind,'RUNNING',:now)"""
            ),
            {"id": specialist_id, "run": run_id, "kind": specialist_type, "now": now},
        )
        await session.execute(
            text(
                """INSERT INTO evidence_records(
                       id,partition_timestamp,observed_at,rca_run_id,specialist_run_id,
                       evidence_type,source_agent,source_endpoint,tool_name,
                       time_window_start,time_window_end,structured_data,content_hash,
                       raw_result,metadata)
                   VALUES (:id,:now,:now,:run,:specialist,:evidence_type,:source_agent,
                           :endpoint,:tool,:now,:now,'{}','hash',
                           CAST('cross-specialist-secret' AS BYTEA),'{}')"""
            ),
            {
                "id": evidence_id,
                "now": now,
                "run": run_id,
                "specialist": specialist_id,
                "evidence_type": f"{endpoint}.query",
                "source_agent": endpoint.upper(),
                "endpoint": endpoint,
                "tool": f"{endpoint}_query",
            },
        )
    return EvidenceReference(id=evidence_id, partition_timestamp=now)


def _analysis(
    reference: EvidenceReference,
    *,
    specialist: SpecialistKind = SpecialistKind.METRICS,
    status: Literal["COMPLETE", "PARTIAL", "FAILED"] = "COMPLETE",
) -> SpecialistAnalysisDraft:
    if status == "FAILED":
        return SpecialistAnalysisDraft(
            specialist=specialist,
            status="FAILED",
            observations=(),
            missing_evidence=("ANALYSIS_FAILED",),
        )
    return SpecialistAnalysisDraft(
        specialist=specialist,
        status=status,
        observations=(
            SpecialistObservation(
                statement="CPU utilization is correlated with query latency",
                confidence=0.91,
                relation="SUPPORTS",
                evidence=(reference,),
            ),
        ),
        missing_evidence=(("ANALYSIS_INPUT_TRUNCATED",) if status == "PARTIAL" else ()),
    )


@pytest.mark.asyncio
async def test_analysis_audit_upsert_persists_only_canonical_owned_analysis() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    run_id, specialist_run_id, reference = await _seed_analysis_owner(sessions)
    analysis = _analysis(reference)

    async with sessions() as session, session.begin():
        repository = RcaRepository(session)
        for _ in range(2):
            await repository.upsert_specialist_analysis(
                rca_run_id=run_id,
                specialist=SpecialistKind.METRICS,
                analysis=analysis,
                model_name="specialist-model-v1",
                skill_name="metrics-analysis",
                skill_sha256="a" * 64,
            )

    async with sessions() as session:
        row = (
            (
                await session.execute(
                    text(
                        """SELECT id,status,failure_code,analysis_result,model_name,
                                  skill_name,skill_sha256,analyzed_at
                           FROM specialist_runs
                           WHERE rca_run_id=:run AND specialist_type='METRICS'"""
                    ),
                    {"run": run_id},
                )
            )
            .mappings()
            .one()
        )
        count = await session.scalar(
            text(
                """SELECT count(*) FROM specialist_runs
                   WHERE rca_run_id=:run AND specialist_type='METRICS'"""
            ),
            {"run": run_id},
        )

    assert row["id"] == specialist_run_id
    assert row["status"] == "SUCCEEDED"
    assert row["failure_code"] is None
    assert row["analysis_result"] == analysis.model_dump(mode="json")
    assert row["model_name"] == "specialist-model-v1"
    assert row["skill_name"] == "metrics-analysis"
    assert row["skill_sha256"] == "a" * 64
    assert row["analyzed_at"] is not None
    assert count == 1
    assert "raw-fixture-secret" not in json.dumps(row["analysis_result"])
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("analysis_status", "row_status", "failure_code"),
    [
        ("PARTIAL", "PARTIAL", "ANALYSIS_INPUT_TRUNCATED"),
        ("FAILED", "FAILED", "ANALYSIS_FAILED"),
    ],
)
async def test_analysis_audit_maps_partial_and_failed_statuses_exactly(
    analysis_status: Literal["PARTIAL", "FAILED"],
    row_status: str,
    failure_code: str,
) -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    run_id, _, reference = await _seed_analysis_owner(sessions)
    analysis = _analysis(reference, status=analysis_status)

    async with sessions() as session, session.begin():
        await RcaRepository(session).upsert_specialist_analysis(
            rca_run_id=run_id,
            specialist=SpecialistKind.METRICS,
            analysis=analysis,
            model_name="specialist-model-v1",
            skill_name="metrics-analysis",
            skill_sha256="c" * 64,
        )

    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    """SELECT status,failure_code,analysis_result
                       FROM specialist_runs
                       WHERE rca_run_id=:run AND specialist_type='METRICS'"""
                ),
                {"run": run_id},
            )
        ).one()

    assert row.status == row_status
    assert row.failure_code == failure_code
    assert row.analysis_result == analysis.model_dump(mode="json")
    await engine.dispose()


@pytest.mark.asyncio
async def test_analysis_audit_ownership_mismatch_never_partially_updates() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    owner_run, _, owner_reference = await _seed_analysis_owner(sessions)
    trace_reference = await _seed_additional_specialist_evidence(
        sessions,
        run_id=owner_run,
        specialist_type="TRACES",
    )
    _, _, other_run_reference = await _seed_analysis_owner(sessions)
    invalid_references = (
        EvidenceReference(
            id=uuid4(),
            partition_timestamp=owner_reference.partition_timestamp,
        ),
        EvidenceReference(
            id=owner_reference.id,
            partition_timestamp=owner_reference.partition_timestamp
            + timedelta(microseconds=1),
        ),
        trace_reference,
        other_run_reference,
    )

    for invalid_reference in invalid_references:
        with pytest.raises(
            SpecialistAnalysisOwnershipError,
            match="ANALYSIS_UNKNOWN_EVIDENCE",
        ):
            async with sessions() as session, session.begin():
                await RcaRepository(session).upsert_specialist_analysis(
                    rca_run_id=owner_run,
                    specialist=SpecialistKind.METRICS,
                    analysis=_analysis(invalid_reference),
                    model_name="specialist-model-v1",
                    skill_name="metrics-analysis",
                    skill_sha256="b" * 64,
                )

    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    """SELECT status,failure_code,analysis_result,model_name,
                              skill_name,skill_sha256,analyzed_at
                       FROM specialist_runs
                       WHERE rca_run_id=:run AND specialist_type='METRICS'"""
                ),
                {"run": owner_run},
            )
        ).one()

    assert row == ("RUNNING", None, None, None, None, None, None)
    await engine.dispose()


@pytest.mark.asyncio
async def test_report_persists_hypothesis_confidence_and_evidence_relations() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    team, project, environment, incident, run, specialist, evidence = [
        uuid4() for _ in range(7)
    ]
    async with sessions() as session, session.begin():
        await session.execute(
            text("INSERT INTO teams(id,name) VALUES (:id,:name)"),
            {"id": team, "name": f"team-{team}"},
        )
        await session.execute(
            text("INSERT INTO projects(id,team_id,name) VALUES (:id,:team,:name)"),
            {"id": project, "team": team, "name": f"project-{project}"},
        )
        await session.execute(
            text(
                "INSERT INTO environments(id,project_id,name) VALUES (:id,:project,:name)"
            ),
            {"id": environment, "project": project, "name": f"env-{environment}"},
        )
        await session.execute(
            text(
                """INSERT INTO incidents(
                       id,identity_key,title,severity,status,alert_state,
                       team_id,project_id,environment_id,opened_at)
                   VALUES (:id,:identity,'test','SEV3','OPEN','FIRING',
                           :team,:project,:environment,:now)"""
            ),
            {
                "id": incident,
                "identity": f"identity-{incident}",
                "team": team,
                "project": project,
                "environment": environment,
                "now": now,
            },
        )
        await session.execute(
            text(
                "INSERT INTO rca_runs(id,incident_id,status) VALUES (:id,:incident,'RUNNING')"
            ),
            {"id": run, "incident": incident},
        )
        await session.execute(
            text(
                """INSERT INTO specialist_runs(
                       id,rca_run_id,specialist_type,status,started_at,completed_at)
                   VALUES (:id,:run,'METRICS','SUCCEEDED',:now,:now)"""
            ),
            {"id": specialist, "run": run, "now": now},
        )
        await session.execute(
            text(
                """INSERT INTO evidence_records(
                       id,partition_timestamp,observed_at,rca_run_id,specialist_run_id,
                       evidence_type,source_agent,source_endpoint,tool_name,
                       time_window_start,time_window_end,structured_data,content_hash,
                       raw_result,metadata)
                   VALUES (:id,:now,:now,:run,:specialist,'METRIC','metrics',
                           'metrics','metrics_query',:now,:now,'{}','hash',
                           CAST('raw' AS BYTEA),'{}')"""
            ),
            {"id": evidence, "now": now, "run": run, "specialist": specialist},
        )

    reference = EvidenceReference(id=evidence, partition_timestamp=now)
    report = RcaReportDraft(
        status="COMPLETE",
        summary_zh_tw="CPU 異常由資料庫負載造成。",
        hypotheses=(
            RcaHypothesis(
                statement="資料庫負載增加",
                confidence=0.88,
                claims=(
                    EvidenceClaim(
                        statement="CPU 與查詢延遲同時上升",
                        relation="SUPPORTS",
                        evidence=(reference,),
                    ),
                ),
            ),
        ),
        missing_evidence=(),
        remediation=("限制高成本查詢",),
        verification_steps=("確認 CPU 與延遲回復",),
    )
    settings = WorkerSettings(
        database_url=SecretStr(DATABASE_URL),
        pubsub_project_id="local",
        rca_topic_id="rca",
        pubsub_subscription_id="worker",
        app_environment="test",
        model_name="test-model",
    )
    claim = RcaJobClaim(
        worker_job_id=uuid4(),
        rca_run_id=run,
        incident_id=incident,
        attempt_number=1,
        deadline_at=now,
        lease_owner="test",
    )
    processor = ProductionRcaProcessor(sessions, settings)
    await processor._persist_failures(
        claim,
        (
            SpecialistFailure(
                specialist=SpecialistKind.TRACE, code="SPECIALIST_TIMEOUT"
            ),
        ),
    )
    await processor._persist_report(claim, report)

    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    """SELECT hypothesis.statement,hypothesis.confidence,link.relation,
                              report.report
                       FROM rca_hypotheses hypothesis
                       JOIN hypothesis_evidence link ON link.hypothesis_id=hypothesis.id
                       JOIN rca_reports report ON report.rca_run_id=hypothesis.rca_run_id
                       WHERE hypothesis.rca_run_id=:run"""
                ),
                {"run": run},
            )
        ).one()
        assert row.statement == "資料庫負載增加"
        assert row.confidence == pytest.approx(0.88)
        assert row.relation == "SUPPORTS"
        assert row.report["confidence"] == pytest.approx(0.88)
        assert row.report["hypotheses"][0]["claims"][0]["evidence"][0][
            "evidenceId"
        ] == str(evidence)
        failed = (
            await session.execute(
                text(
                    """SELECT status,failure_code FROM specialist_runs
                       WHERE rca_run_id=:run AND specialist_type='TRACES'"""
                ),
                {"run": run},
            )
        ).one()
        assert failed == ("FAILED", "MCP_TIMEOUT")
    await engine.dispose()
