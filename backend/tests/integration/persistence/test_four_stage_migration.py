"""Acceptance coverage for the immutable four-gate migration rollout."""

import asyncio
import hashlib
import importlib
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[3]
REPOSITORY_ROOT = BACKEND_ROOT.parent
WORKER_ROOT = REPOSITORY_ROOT / "rca-worker"
DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent",
)
RAW_RESULT = b'\x00\xffnon-utf8\n{"a":1.00}'
METADATA = {"contentType": "application/json", "source": "metrics"}
CONTENT_HASH = hashlib.sha256(RAW_RESULT).hexdigest()


def _asyncpg_url(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _with_database(database_url: str, database_name: str) -> str:
    parsed = urlsplit(database_url)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database_name}", "", ""))


def _upgrade(root: Path, database_url: str, revision: str) -> None:
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    with patch.dict(os.environ, {"MIGRATION_TEST_DATABASE_URL": database_url}):
        command.upgrade(config, revision)


def upgrade_backend(database_url: str, revision: str) -> None:
    _upgrade(BACKEND_ROOT, database_url, revision)


def upgrade_worker(database_url: str, revision: str) -> None:
    _upgrade(WORKER_ROOT, database_url, revision)


async def upgrade_four_gates(database_url: str, *, existing_worker_head: bool) -> None:
    """Run all explicit rollout targets without stamping or using ``head``."""
    if not existing_worker_head:
        await asyncio.to_thread(
            upgrade_backend, database_url, "0002_grafana_normalization_v2"
        )
        await asyncio.to_thread(
            upgrade_worker, database_url, "0002_adk_specialist_analysis"
        )
    await asyncio.to_thread(
        upgrade_backend, database_url, "0003_non_partition_runtime_tables"
    )
    await asyncio.to_thread(
        upgrade_worker,
        database_url,
        "0003_validate_ordinary_runtime_tables",
    )


async def _connect(database_url: str) -> asyncpg.Connection:
    connection = await asyncpg.connect(_asyncpg_url(database_url))
    await connection.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=json.dumps,
        decoder=json.loads,
    )
    return connection


@asynccontextmanager
async def _disposable_database() -> AsyncIterator[str]:
    parsed = urlsplit(DATABASE_URL)
    if parsed.hostname != "127.0.0.1" or parsed.port != 5432:
        raise RuntimeError(
            "Four-stage tests require disposable PostgreSQL at 127.0.0.1:5432"
        )
    database_name = f"task1_four_stage_{uuid4().hex}"
    admin = await asyncpg.connect(
        _asyncpg_url(_with_database(DATABASE_URL, "postgres"))
    )
    database_url = _with_database(DATABASE_URL, database_name)
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        created = True
        yield database_url
    finally:
        if created:
            await admin.execute(
                f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
            )
        await admin.close()


def _month_pair() -> tuple[datetime, datetime]:
    current = datetime.now(UTC).replace(
        day=1, hour=12, minute=0, second=0, microsecond=0
    )
    if current.month == 12:
        return current, current.replace(year=current.year + 1, month=1)
    return current, current.replace(month=current.month + 1)


async def _seed_worker_0002_source(
    connection: asyncpg.Connection,
) -> dict[str, UUID]:
    ids = {
        "team": UUID("20000000-0000-0000-0000-000000000001"),
        "project": UUID("20000000-0000-0000-0000-000000000002"),
        "environment": UUID("20000000-0000-0000-0000-000000000003"),
        "service": UUID("20000000-0000-0000-0000-000000000004"),
        "incident": UUID("20000000-0000-0000-0000-000000000005"),
        "run": UUID("20000000-0000-0000-0000-000000000006"),
        "specialist": UUID("20000000-0000-0000-0000-000000000007"),
        "report": UUID("20000000-0000-0000-0000-000000000008"),
        "job": UUID("20000000-0000-0000-0000-000000000009"),
        "attempt": UUID("20000000-0000-0000-0000-000000000010"),
        "evidence_one": UUID("20000000-0000-0000-0000-000000000011"),
        "evidence_two": UUID("20000000-0000-0000-0000-000000000012"),
    }
    first_month, second_month = _month_pair()
    await connection.execute(
        "INSERT INTO teams (id, name) VALUES ($1, 'task1-team')", ids["team"]
    )
    await connection.execute(
        "INSERT INTO projects (id, team_id, name) VALUES ($1, $2, 'task1-project')",
        ids["project"],
        ids["team"],
    )
    await connection.execute(
        "INSERT INTO environments (id, project_id, name) VALUES ($1, $2, 'task1-environment')",
        ids["environment"],
        ids["project"],
    )
    await connection.execute(
        "INSERT INTO services (id, environment_id, name) VALUES ($1, $2, 'task1-service')",
        ids["service"],
        ids["environment"],
    )
    await connection.execute(
        """INSERT INTO incidents (
               id, identity_key, title, severity, status, alert_state, team_id,
               project_id, environment_id, service_id, opened_at
           ) VALUES (
               $1, 'task1-identity', 'task1 incident', 'SEV3', 'OPEN',
               'FIRING', $2, $3, $4, $5, now()
           )""",
        ids["incident"],
        ids["team"],
        ids["project"],
        ids["environment"],
        ids["service"],
    )
    await connection.execute(
        """INSERT INTO rca_runs (id, incident_id, status, failure_code)
           VALUES ($1, $2, 'FAILED', 'MCP_TIMEOUT')""",
        ids["run"],
        ids["incident"],
    )
    await connection.execute(
        """INSERT INTO specialist_runs (
               id, rca_run_id, specialist_type, status, failure_code,
               analysis_result, model_name, skill_name, skill_sha256, analyzed_at
           ) VALUES (
               $1, $2, 'METRICS', 'PARTIAL', 'MCP_TIMEOUT', $3,
               'gemini-test', 'metrics-analysis', $4, now()
           )""",
        ids["specialist"],
        ids["run"],
        {"summary": "partial analysis", "evidenceIds": []},
        "a" * 64,
    )
    await connection.execute(
        """INSERT INTO rca_reports (
               id, rca_run_id, version, summary, report, result_status
           ) VALUES ($1, $2, 1, 'task1 report', $3, 'PARTIAL')""",
        ids["report"],
        ids["run"],
        {"status": "PARTIAL"},
    )
    await connection.execute(
        """INSERT INTO worker_jobs (
               id, rca_run_id, job_type, status, payload, lease_owner,
               lease_expires_at, attempt_count
           ) VALUES (
               $1, $2, 'RCA', 'FAILED', '{}'::jsonb, NULL, NULL, 1
           )""",
        ids["job"],
        ids["run"],
    )
    await connection.execute(
        """INSERT INTO worker_attempts (
               id, worker_job_id, attempt_number, failure_code
           ) VALUES ($1, $2, 1, 'MCP_TIMEOUT')""",
        ids["attempt"],
        ids["job"],
    )
    for evidence_id, timestamp in (
        (ids["evidence_one"], first_month),
        (ids["evidence_two"], second_month),
    ):
        await connection.execute(
            """INSERT INTO evidence_records (
                   id, partition_timestamp, observed_at, rca_run_id,
                   specialist_run_id, evidence_type, source_agent,
                   source_endpoint, tool_name, team_id, project_id,
                   environment_id, service_id, time_window_start,
                   time_window_end, structured_data, raw_result, metadata,
                   content_hash
               ) VALUES (
                   $1, $2, $2, $3, $4, 'METRIC', 'metrics', 'mcp://metrics',
                   'query_range', $5, $6, $7, $8, $2, $2, '{}'::jsonb,
                   $9, $10, $11
               )""",
            evidence_id,
            timestamp,
            ids["run"],
            ids["specialist"],
            ids["team"],
            ids["project"],
            ids["environment"],
            ids["service"],
            RAW_RESULT,
            METADATA,
            CONTENT_HASH,
        )
    return ids


async def _worker_catalog_snapshot(
    connection: asyncpg.Connection,
) -> tuple[object, ...]:
    table_names = (
        "rca_reports",
        "rca_runs",
        "worker_jobs",
        "worker_attempts",
        "specialist_runs",
    )
    table_oids = await connection.fetch(
        """SELECT relname, oid::bigint
           FROM pg_class
           WHERE oid = ANY($1::regclass[])
           ORDER BY relname""",
        list(table_names),
    )
    columns = await connection.fetch(
        """SELECT table_name, column_name, udt_name, is_nullable, column_default
           FROM information_schema.columns
           WHERE table_schema = 'public'
             AND table_name = ANY($1::text[])
           ORDER BY table_name, ordinal_position""",
        list(table_names),
    )
    constraints = await connection.fetch(
        """SELECT relation.relname, constraint_row.conname,
                  constraint_row.contype,
                  pg_get_constraintdef(constraint_row.oid, true)
           FROM pg_constraint AS constraint_row
           JOIN pg_class AS relation
             ON relation.oid = constraint_row.conrelid
           WHERE relation.relname = ANY($1::text[])
           ORDER BY relation.relname, constraint_row.conname""",
        list(table_names),
    )
    indexes = await connection.fetch(
        """SELECT tablename, indexname, indexdef
           FROM pg_indexes
           WHERE schemaname = 'public'
             AND tablename = ANY($1::text[])
           ORDER BY tablename, indexname""",
        list(table_names),
    )
    return tuple(
        tuple(row)
        for rows in (table_oids, columns, constraints, indexes)
        for row in rows
    )


async def _worker_data_snapshot(
    connection: asyncpg.Connection, ids: dict[str, UUID]
) -> tuple[tuple[object, ...], ...]:
    queries = (
        (
            "SELECT status, failure_code FROM rca_runs WHERE id=$1",
            ids["run"],
        ),
        (
            "SELECT result_status, report FROM rca_reports WHERE id=$1",
            ids["report"],
        ),
        (
            """SELECT status, lease_owner, lease_expires_at, attempt_count
               FROM worker_jobs WHERE id=$1""",
            ids["job"],
        ),
        (
            """SELECT attempt_number, failure_code
               FROM worker_attempts WHERE id=$1""",
            ids["attempt"],
        ),
        (
            """SELECT status, failure_code, analysis_result, model_name,
                      skill_name, skill_sha256, analyzed_at
               FROM specialist_runs WHERE id=$1""",
            ids["specialist"],
        ),
    )
    snapshot: list[tuple[object, ...]] = []
    for query, row_id in queries:
        row = await connection.fetchrow(query, row_id)
        assert row is not None
        snapshot.append(tuple(row))
    return tuple(snapshot)


async def table_oid(connection: asyncpg.Connection, table_name: str) -> int:
    oid = await connection.fetchval(
        "SELECT to_regclass($1)::oid::bigint", f"public.{table_name}"
    )
    assert isinstance(oid, int)
    return oid


async def _upgrade_to_worker_0002(database_url: str) -> asyncpg.Connection:
    await asyncio.to_thread(
        upgrade_backend, database_url, "0002_grafana_normalization_v2"
    )
    await asyncio.to_thread(
        upgrade_worker, database_url, "0002_adk_specialist_analysis"
    )
    return await _connect(database_url)


async def _assert_backend_0003_fails_before_replacement(
    database_url: str,
    connection: asyncpg.Connection,
    *,
    match: str,
) -> asyncpg.Connection:
    original_oid = await table_oid(connection, "evidence_records")
    await connection.close()
    with pytest.raises(RuntimeError, match=match):
        await asyncio.to_thread(
            upgrade_backend, database_url, "0003_non_partition_runtime_tables"
        )
    connection = await _connect(database_url)
    assert await table_oid(connection, "evidence_records") == original_oid
    assert (
        await connection.fetchval("SELECT to_regclass('public.evidence_records_new')")
        is None
    )
    assert (
        await connection.fetchval("SELECT version_num FROM alembic_version_backend")
        == "0002_grafana_normalization_v2"
    )
    return connection


@pytest.mark.asyncio
async def test_backend_0003_preserves_worker_0002_evidence_and_lifecycle() -> None:
    async with _disposable_database() as database_url:
        await asyncio.to_thread(
            upgrade_backend, database_url, "0002_grafana_normalization_v2"
        )
        await asyncio.to_thread(
            upgrade_worker, database_url, "0002_adk_specialist_analysis"
        )
        connection = await _connect(database_url)
        ids = await _seed_worker_0002_source(connection)

        before = await connection.fetchrow(
            """SELECT raw_result, metadata, content_hash
               FROM evidence_records
               WHERE id = $1""",
            ids["evidence_one"],
        )
        assert before is not None
        assert bytes(before["raw_result"]) == RAW_RESULT
        assert before["metadata"] == METADATA
        source_columns = {
            row["column_name"]
            for row in await connection.fetch(
                """SELECT column_name
                   FROM information_schema.columns
                   WHERE table_schema = 'public'
                     AND table_name = 'evidence_records'"""
            )
        }
        assert {"raw_result", "metadata", "content_hash"} <= source_columns
        assert "raw_result_reference" not in source_columns
        assert (
            await connection.fetchval(
                "SELECT version_num FROM alembic_version_rca_worker"
            )
            == "0002_adk_specialist_analysis"
        )
        assert (
            await connection.fetchval(
                "SELECT result_status FROM rca_reports WHERE rca_run_id=$1", ids["run"]
            )
            == "PARTIAL"
        )
        catalog_before = await _worker_catalog_snapshot(connection)
        worker_data_before = await _worker_data_snapshot(connection, ids)
        await connection.close()

        await asyncio.to_thread(
            upgrade_backend, database_url, "0003_non_partition_runtime_tables"
        )
        await asyncio.to_thread(
            upgrade_worker,
            database_url,
            "0003_validate_ordinary_runtime_tables",
        )
        connection = await _connect(database_url)
        try:
            rows = await connection.fetch(
                """SELECT raw_result, metadata, content_hash
                   FROM evidence_records
                   ORDER BY observed_at"""
            )
            assert len(rows) == 2
            assert all(bytes(row["raw_result"]) == RAW_RESULT for row in rows)
            assert all(row["metadata"] == METADATA for row in rows)
            assert all(row["content_hash"] == CONTENT_HASH for row in rows)
            assert (
                await connection.fetchval(
                    "SELECT result_status FROM rca_reports WHERE rca_run_id=$1",
                    ids["run"],
                )
                == "PARTIAL"
            )
            assert (
                await connection.fetchval(
                    "SELECT failure_code FROM rca_runs WHERE id=$1", ids["run"]
                )
                == "MCP_TIMEOUT"
            )
            assert await connection.fetchval(
                "SELECT analysis_result FROM specialist_runs WHERE id=$1",
                ids["specialist"],
            ) == {"summary": "partial analysis", "evidenceIds": []}
            assert await _worker_catalog_snapshot(connection) == catalog_before
            assert await _worker_data_snapshot(connection, ids) == worker_data_before
            assert (
                await connection.fetchval(
                    "SELECT version_num FROM alembic_version_backend"
                )
                == "0003_non_partition_runtime_tables"
            )
            assert (
                await connection.fetchval(
                    "SELECT version_num FROM alembic_version_rca_worker"
                )
                == "0003_validate_ordinary_runtime_tables"
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_backend_0003_requires_worker_0002_revision() -> None:
    async with _disposable_database() as database_url:
        await asyncio.to_thread(
            upgrade_backend, database_url, "0002_grafana_normalization_v2"
        )
        await asyncio.to_thread(upgrade_worker, database_url, "0001_rca_worker_v1")
        connection = await _connect(database_url)
        try:
            connection = await _assert_backend_0003_fails_before_replacement(
                database_url,
                connection,
                match="Worker 0002_adk_specialist_analysis is required",
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            "ALTER TABLE evidence_records DROP COLUMN metadata",
            "evidence_records is not the Worker 0002 source schema",
        ),
        (
            "ALTER TABLE rca_reports DROP CONSTRAINT ck_rca_reports_result_status",
            "rca_reports.result_status Worker 0002 check is required",
        ),
        (
            """ALTER TABLE worker_jobs
               DROP CONSTRAINT ck_worker_jobs_attempt_count,
               DROP COLUMN attempt_count""",
            "worker_jobs Worker 0002 lifecycle schema is required",
        ),
        (
            "ALTER TABLE rca_runs DROP COLUMN failure_code",
            "Worker 0002 failure_code lifecycle schema is required",
        ),
        (
            """ALTER TABLE specialist_runs
               DROP CONSTRAINT ck_specialist_runs_analysis_result_object""",
            "specialist_runs Worker 0002 analysis schema is required",
        ),
    ),
)
async def test_backend_0003_rejects_incomplete_worker_0002_catalog(
    mutation: str,
    message: str,
) -> None:
    async with _disposable_database() as database_url:
        connection = await _upgrade_to_worker_0002(database_url)
        try:
            await connection.execute(mutation)
            connection = await _assert_backend_0003_fails_before_replacement(
                database_url, connection, match=message
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_backend_0003_rejects_duplicate_evidence_uuid_before_replacement() -> (
    None
):
    async with _disposable_database() as database_url:
        connection = await _upgrade_to_worker_0002(database_url)
        ids = await _seed_worker_0002_source(connection)
        _, second_month = _month_pair()
        await connection.execute(
            """INSERT INTO evidence_records (
                   id, partition_timestamp, observed_at, rca_run_id,
                   specialist_run_id, evidence_type, source_agent,
                   source_endpoint, tool_name, team_id, project_id,
                   environment_id, service_id, time_window_start,
                   time_window_end, structured_data, raw_result, metadata,
                   content_hash
               ) VALUES (
                   $1, $2, $2, $3, $4, 'METRIC', 'metrics', 'mcp://metrics',
                   'query_range', $5, $6, $7, $8, $2, $2, '{}'::jsonb,
                   $9, $10, $11
               )""",
            ids["evidence_one"],
            second_month,
            ids["run"],
            ids["specialist"],
            ids["team"],
            ids["project"],
            ids["environment"],
            ids["service"],
            RAW_RESULT,
            METADATA,
            CONTENT_HASH,
        )
        original_oid = await table_oid(connection, "evidence_records")
        await connection.close()
        with pytest.raises(Exception, match="duplicate UUID precheck failed"):
            await asyncio.to_thread(
                upgrade_backend, database_url, "0003_non_partition_runtime_tables"
            )
        connection = await _connect(database_url)
        try:
            assert await table_oid(connection, "evidence_records") == original_oid
            assert (
                await connection.fetchval(
                    "SELECT to_regclass('public.evidence_records_new')"
                )
                is None
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_backend_0003_matching_rerun_is_catalog_no_op() -> None:
    async with _disposable_database() as database_url:
        await upgrade_four_gates(database_url, existing_worker_head=False)
        connection = await _connect(database_url)
        before = (
            await table_oid(connection, "evidence_records"),
            await _worker_catalog_snapshot(connection),
        )
        await connection.close()

        await asyncio.to_thread(
            upgrade_backend, database_url, "0003_non_partition_runtime_tables"
        )
        connection = await _connect(database_url)
        try:
            assert (
                await table_oid(connection, "evidence_records"),
                await _worker_catalog_snapshot(connection),
            ) == before
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_clean_four_gate_database_runs_uuid_only_worker_smoke() -> None:
    """Exercise Worker persistence only after all four explicit rollout gates."""
    async with _disposable_database() as database_url:
        await upgrade_four_gates(database_url, existing_worker_head=False)
        connection = await _connect(database_url)
        now = datetime(2026, 8, 28, 8, 30, tzinfo=UTC)
        team_id, project_id, environment_id, incident_id, run_id, specialist_id = (
            uuid4() for _ in range(6)
        )
        try:
            assert (
                await connection.fetchval(
                    "SELECT version_num FROM alembic_version_backend"
                )
                == "0003_non_partition_runtime_tables"
            )
            assert (
                await connection.fetchval(
                    "SELECT version_num FROM alembic_version_rca_worker"
                )
                == "0003_validate_ordinary_runtime_tables"
            )
            await connection.execute(
                "INSERT INTO teams(id,name) VALUES ($1,$2)",
                team_id,
                f"task3-team-{team_id}",
            )
            await connection.execute(
                "INSERT INTO projects(id,team_id,name) VALUES ($1,$2,$3)",
                project_id,
                team_id,
                f"task3-project-{project_id}",
            )
            await connection.execute(
                "INSERT INTO environments(id,project_id,name) VALUES ($1,$2,$3)",
                environment_id,
                project_id,
                f"task3-environment-{environment_id}",
            )
            await connection.execute(
                """INSERT INTO incidents(
                       id,identity_key,title,severity,status,alert_state,
                       team_id,project_id,environment_id,opened_at)
                   VALUES ($1,$2,'task3 smoke','SEV3','OPEN','FIRING',
                           $3,$4,$5,$6)""",
                incident_id,
                f"task3-identity-{incident_id}",
                team_id,
                project_id,
                environment_id,
                now,
            )
            await connection.execute(
                """INSERT INTO rca_runs(id,incident_id,status)
                   VALUES ($1,$2,'RUNNING')""",
                run_id,
                incident_id,
            )
            await connection.execute(
                """INSERT INTO specialist_runs(
                       id,rca_run_id,specialist_type,status,started_at)
                   VALUES ($1,$2,'METRICS','RUNNING',$3)""",
                specialist_id,
                run_id,
                now,
            )
        finally:
            await connection.close()

        worker_source = str(WORKER_ROOT / "src")
        if worker_source not in sys.path:
            sys.path.insert(0, worker_source)
        job_module = importlib.import_module(
            "sre_rca_worker.application.rca.job_lifecycle"
        )
        processor_module = importlib.import_module(
            "sre_rca_worker.application.rca.processor"
        )
        settings_module = importlib.import_module("sre_rca_worker.config.settings")
        evidence_module = importlib.import_module(
            "sre_rca_worker.domain.evidence.models"
        )
        report_module = importlib.import_module("sre_rca_worker.domain.rca.models")
        mcp_module = importlib.import_module("sre_rca_worker.integrations.mcp.models")
        repository_module = importlib.import_module(
            "sre_rca_worker.persistence.repositories.rca"
        )

        raw_result = (
            b'\x00\xffnon-utf8\n{ "b": 1.00, "a": "\\u0061", "a": "duplicate" }\n'
        )
        input_sha256 = "7" * 64
        expected_metadata = {
            "contentType": "application/json",
            "inputSha256": input_sha256,
            "inputScope": {
                "provider": "GCP",
                "scope_id": "task3-project",
                "safe": True,
            },
            "normalizedScope": {
                "provider": "GCP",
                "scope_id": "task3-project",
                "safe": True,
            },
            "requestWindowStart": "2026-08-28T08:15:00+00:00",
            "requestWindowEnd": "2026-08-28T08:30:00+00:00",
        }
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            scope = mcp_module.CloudScope(
                provider="GCP", scope_id="task3-project", safe=True
            )
            draft = evidence_module.EvidenceDraft(
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
                raw_result=raw_result,
                content_type="application/json",
                input_sha256=input_sha256,
            )
            async with sessions() as session, session.begin():
                repository = repository_module.RcaRepository(session)
                reference = await repository.insert_evidence(
                    run_id, specialist_id, draft
                )
                persisted = await repository.get_specialist_evidence(
                    run_id, specialist_id, reference.id
                )
                assert persisted is not None
                assert persisted.reference.model_dump(mode="json") == {
                    "id": str(reference.id)
                }
                assert persisted.structured_data == {"a": "duplicate", "b": 1.0}
                assert not hasattr(persisted, "raw_result")
                assert not hasattr(persisted, "metadata")

            partial_report = report_module.RcaReportDraft(
                status="PARTIAL",
                summary_zh_tw="Task 3 canonical smoke report",
                hypotheses=(
                    report_module.RcaHypothesis(
                        statement="metrics spike",
                        confidence=0.75,
                        claims=(
                            report_module.EvidenceClaim(
                                statement="metric remained elevated",
                                relation="SUPPORTS",
                                evidence=(reference,),
                            ),
                        ),
                    ),
                ),
                missing_evidence=("trace evidence unavailable",),
                remediation=("reduce load",),
                verification_steps=("verify metrics recover",),
            )
            settings = settings_module.WorkerSettings(
                database_url=SecretStr(database_url),
                pubsub_project_id="task3-local",
                rca_topic_id="task3-rca",
                pubsub_subscription_id="task3-worker",
                app_environment="test",
                model_name="task3-model",
            )
            processor = processor_module.ProductionRcaProcessor(sessions, settings)
            claim = job_module.RcaJobClaim(
                worker_job_id=uuid4(),
                rca_run_id=run_id,
                incident_id=incident_id,
                attempt_number=1,
                deadline_at=now + timedelta(minutes=5),
                lease_owner="task3-smoke",
            )
            await processor._persist_report(claim, partial_report)

            async with sessions() as session:
                evidence_row = (
                    (
                        await session.execute(
                            text(
                                """SELECT raw_result,metadata,content_hash
                                   FROM evidence_records WHERE id=:id"""
                            ),
                            {"id": reference.id},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert bytes(evidence_row["raw_result"]) == raw_result
                assert evidence_row["metadata"] == expected_metadata
                assert (
                    evidence_row["content_hash"]
                    == hashlib.sha256(raw_result).hexdigest()
                )
                report_row = (
                    await session.execute(
                        text(
                            """SELECT version,result_status,report
                               FROM rca_reports WHERE rca_run_id=:run"""
                        ),
                        {"run": run_id},
                    )
                ).one()
                assert report_row.version == 1
                assert report_row.result_status == "PARTIAL"
                assert report_row.report["hypotheses"][0]["claims"][0]["evidence"] == [
                    {
                        "evidenceId": str(reference.id),
                        "relation": "SUPPORTS",
                    }
                ]
        finally:
            await engine.dispose()
