import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from sre_rca_worker.application.rca.persist_evidence import PersistEvidence
from sre_rca_worker.domain.evidence.models import EvidenceDraft
from sre_rca_worker.integrations.mcp.models import CloudScope

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
)


@pytest.mark.asyncio
async def test_persist_evidence_round_trips_exact_bytes_and_provenance() -> None:
    engine = create_async_engine(DATABASE_URL)
    now = datetime(2026, 8, 13, 6, 30, tzinfo=UTC)
    raw = b'\x00\xffnon-utf8\n{ "b": 1.00, "a": "\\u0061", "a": "duplicate" }\n'
    team_id, project_id, environment_id, incident_id, run_id = [
        uuid4() for _ in range(5)
    ]
    specialist_id = uuid4()
    async with AsyncSession(engine) as session, session.begin():
        await session.execute(
            text("INSERT INTO teams(id,name) VALUES (:id,:name)"),
            {"id": team_id, "name": f"team-{team_id}"},
        )
        await session.execute(
            text("INSERT INTO projects(id,team_id,name) VALUES (:id,:team,:name)"),
            {"id": project_id, "team": team_id, "name": f"project-{project_id}"},
        )
        await session.execute(
            text(
                "INSERT INTO environments(id,project_id,name) VALUES (:id,:project,:name)"
            ),
            {
                "id": environment_id,
                "project": project_id,
                "name": f"env-{environment_id}",
            },
        )
        await session.execute(
            text("""INSERT INTO incidents(id,identity_key,title,severity,status,alert_state,team_id,project_id,environment_id,opened_at)
            VALUES (:id,:identity,'test','SEV3','OPEN','FIRING',:team,:project,:environment,:now)"""),
            {
                "id": incident_id,
                "identity": f"identity-{incident_id}",
                "team": team_id,
                "project": project_id,
                "environment": environment_id,
                "now": now,
            },
        )
        await session.execute(
            text(
                "INSERT INTO rca_runs(id,incident_id,status) VALUES (:id,:incident,'RUNNING')"
            ),
            {"id": run_id, "incident": incident_id},
        )
        await session.execute(
            text(
                "INSERT INTO specialist_runs(id,rca_run_id,specialist_type,status) VALUES (:id,:run,'METRICS','RUNNING')"
            ),
            {"id": specialist_id, "run": run_id},
        )
        scope = CloudScope(provider="GCP", scope_id="project-a", safe=True)
        draft = EvidenceDraft(
            endpoint_identity="metrics",
            capability="metrics.query",
            tool="metrics_query",
            input_scope=scope,
            normalized_scope=scope,
            observed_at=now,
            request_window_start=now - timedelta(minutes=15),
            request_window_end=now,
            window_start=now - timedelta(minutes=15),
            window_end=now,
            structured_json={"a": "duplicate", "b": 1.0},
            raw_result=raw,
            content_type="application/json",
            input_sha256="a" * 64,
        )
        reference = await PersistEvidence(session).save(run_id, specialist_id, draft)
        row = (
            (
                await session.execute(
                    text("""SELECT raw_result, structured_data, metadata, content_hash
            FROM evidence_records WHERE id=:id"""),
                    {"id": reference.id},
                )
            )
            .mappings()
            .one()
        )
        assert bytes(row["raw_result"]) == raw
        assert row["structured_data"] == {"a": "duplicate", "b": 1.0}
        assert row["metadata"] == {
            "contentType": "application/json",
            "inputSha256": "a" * 64,
            "inputScope": {
                "provider": "GCP",
                "scope_id": "project-a",
                "safe": True,
            },
            "normalizedScope": {
                "provider": "GCP",
                "scope_id": "project-a",
                "safe": True,
            },
            "requestWindowStart": "2026-08-13T06:15:00+00:00",
            "requestWindowEnd": "2026-08-13T06:30:00+00:00",
        }
        assert row["content_hash"] == hashlib.sha256(raw).hexdigest()
        assert reference.model_dump(mode="json") == {"id": str(reference.id)}
        await session.rollback()
    await engine.dispose()
