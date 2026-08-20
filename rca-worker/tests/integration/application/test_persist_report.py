import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sre_rca_worker.agents.rca.models import SpecialistFailure
from sre_rca_worker.application.rca.job_lifecycle import RcaJobClaim
from sre_rca_worker.application.rca.processor import ProductionRcaProcessor
from sre_rca_worker.config.settings import WorkerSettings
from sre_rca_worker.domain.evidence.models import EvidenceReference
from sre_rca_worker.domain.rca.models import (
    EvidenceClaim,
    RcaHypothesis,
    RcaReportDraft,
)
from sre_rca_worker.integrations.mcp.models import SpecialistKind

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
)


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
