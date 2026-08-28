"""Acceptance coverage for the Worker post-conversion migration gate."""

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine

WORKER_ROOT = Path(__file__).resolve().parents[3]
REPOSITORY_ROOT = WORKER_ROOT.parent
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT / "src"))
DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent",
)
RAW_RESULT = b'\x00\xffnon-utf8\n{"a":1.00}'
METADATA = {"contentType": "application/json", "source": "metrics"}
CONTENT_HASH = hashlib.sha256(RAW_RESULT).hexdigest()
CANONICAL_TABLES = (
    "webhook_deliveries",
    "alert_events",
    "evidence_records",
    "incident_messages",
    "incident_timeline_events",
    "audit_events",
)
WORKER_CATALOG_TABLES = (
    "evidence_records",
    "hypothesis_evidence",
    "rca_reports",
    "rca_runs",
    "worker_jobs",
    "worker_attempts",
    "specialist_runs",
)


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


def downgrade_worker(database_url: str, revision: str) -> None:
    config = Config(str(WORKER_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(WORKER_ROOT / "migrations"))
    with patch.dict(os.environ, {"MIGRATION_TEST_DATABASE_URL": database_url}):
        command.downgrade(config, revision)


@asynccontextmanager
async def _disposable_database() -> AsyncIterator[str]:
    parsed = urlsplit(DATABASE_URL)
    if parsed.hostname != "127.0.0.1" or parsed.port != 5432:
        raise RuntimeError(
            "Worker gate tests require disposable PostgreSQL at 127.0.0.1:5432"
        )
    database_name = f"task2_worker_gate_{uuid4().hex}"
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


async def _connect(database_url: str) -> asyncpg.Connection:
    connection = await asyncpg.connect(_asyncpg_url(database_url))
    await connection.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=json.dumps,
        decoder=json.loads,
    )
    return connection


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
        "team": UUID("30000000-0000-0000-0000-000000000001"),
        "project": UUID("30000000-0000-0000-0000-000000000002"),
        "environment": UUID("30000000-0000-0000-0000-000000000003"),
        "service": UUID("30000000-0000-0000-0000-000000000004"),
        "incident": UUID("30000000-0000-0000-0000-000000000005"),
        "run": UUID("30000000-0000-0000-0000-000000000006"),
        "specialist": UUID("30000000-0000-0000-0000-000000000007"),
        "report": UUID("30000000-0000-0000-0000-000000000008"),
        "job": UUID("30000000-0000-0000-0000-000000000009"),
        "attempt": UUID("30000000-0000-0000-0000-000000000010"),
        "evidence_one": UUID("30000000-0000-0000-0000-000000000011"),
        "evidence_two": UUID("30000000-0000-0000-0000-000000000012"),
    }
    first_month, second_month = _month_pair()
    await connection.execute(
        "INSERT INTO teams (id, name) VALUES ($1, 'task2-team')", ids["team"]
    )
    await connection.execute(
        "INSERT INTO projects (id, team_id, name) VALUES ($1, $2, 'task2-project')",
        ids["project"],
        ids["team"],
    )
    await connection.execute(
        "INSERT INTO environments (id, project_id, name) VALUES ($1, $2, 'task2-environment')",
        ids["environment"],
        ids["project"],
    )
    await connection.execute(
        "INSERT INTO services (id, environment_id, name) VALUES ($1, $2, 'task2-service')",
        ids["service"],
        ids["environment"],
    )
    await connection.execute(
        """INSERT INTO incidents (
               id, identity_key, title, severity, status, alert_state, team_id,
               project_id, environment_id, service_id, opened_at
           ) VALUES (
               $1, 'task2-identity', 'task2 incident', 'SEV3', 'OPEN',
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
           ) VALUES ($1, $2, 1, 'task2 report', $3, 'PARTIAL')""",
        ids["report"],
        ids["run"],
        {"status": "PARTIAL"},
    )
    await connection.execute(
        """INSERT INTO worker_jobs (
               id, rca_run_id, job_type, status, payload, lease_owner,
               lease_expires_at, attempt_count
           ) VALUES ($1, $2, 'RCA', 'FAILED', '{}'::jsonb, NULL, NULL, 1)""",
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


async def _catalog_snapshot(
    connection: asyncpg.Connection,
) -> tuple[tuple[object, ...], ...]:
    rows = await connection.fetch(
        """SELECT 'column', table_name, column_name, udt_name, is_nullable,
                  character_maximum_length::text
           FROM information_schema.columns
           WHERE table_schema = 'public'
             AND table_name = ANY($1::text[])
           UNION ALL
           SELECT 'constraint', relation.relname, constraint_row.conname,
                  constraint_row.contype::text,
                  pg_get_constraintdef(constraint_row.oid, true), NULL
           FROM pg_constraint AS constraint_row
           JOIN pg_class AS relation
             ON relation.oid = constraint_row.conrelid
           WHERE relation.relname = ANY($1::text[])
           UNION ALL
           SELECT 'relation', relation.relname, relation.relkind::text,
                  relation.relispartition::text, NULL, NULL
           FROM pg_class AS relation
           JOIN pg_namespace AS namespace
             ON namespace.oid = relation.relnamespace
           WHERE namespace.nspname = 'public'
             AND relation.relname = ANY($1::text[])
           ORDER BY 1, 2, 3""",
        [
            *CANONICAL_TABLES,
            *WORKER_CATALOG_TABLES,
            *(f"{table}__partitioned_legacy_0003" for table in CANONICAL_TABLES),
            "alembic_version_backend",
            "alembic_version_rca_worker",
        ],
    )
    return tuple(tuple(row) for row in rows)


async def _data_snapshot(
    connection: asyncpg.Connection, ids: dict[str, UUID]
) -> tuple[tuple[object, ...], ...]:
    evidence_rows = await connection.fetch(
        """SELECT id, observed_at, raw_result, metadata, content_hash
           FROM evidence_records ORDER BY id"""
    )
    legacy_evidence_rows = await connection.fetch(
        """SELECT id, observed_at, raw_result, metadata, content_hash
           FROM evidence_records__partitioned_legacy_0003 ORDER BY id"""
    )
    worker_rows = (
        await connection.fetchrow(
            "SELECT status, failure_code FROM rca_runs WHERE id=$1", ids["run"]
        ),
        await connection.fetchrow(
            "SELECT result_status, report FROM rca_reports WHERE id=$1", ids["report"]
        ),
        await connection.fetchrow(
            """SELECT status, lease_owner, lease_expires_at, attempt_count
               FROM worker_jobs WHERE id=$1""",
            ids["job"],
        ),
        await connection.fetchrow(
            "SELECT attempt_number, failure_code FROM worker_attempts WHERE id=$1",
            ids["attempt"],
        ),
        await connection.fetchrow(
            """SELECT status, failure_code, analysis_result, model_name,
                      skill_name, skill_sha256, analyzed_at
               FROM specialist_runs WHERE id=$1""",
            ids["specialist"],
        ),
    )
    assert all(row is not None for row in worker_rows)
    return tuple(
        tuple(row)
        for row in (*evidence_rows, *legacy_evidence_rows, *worker_rows)
        if row is not None
    )


async def _assert_final_catalog(connection: asyncpg.Connection) -> None:
    versions = await connection.fetch(
        """SELECT 'backend', version_num FROM alembic_version_backend
           UNION ALL
           SELECT 'worker', version_num FROM alembic_version_rca_worker
           ORDER BY 1"""
    )
    assert [tuple(row) for row in versions] == [
        ("backend", "0003_non_partition_runtime_tables"),
        ("worker", "0003_validate_ordinary_runtime_tables"),
    ]
    version_lengths = await connection.fetch(
        """SELECT table_name, character_maximum_length
           FROM information_schema.columns
           WHERE table_schema = 'public'
             AND table_name IN (
                 'alembic_version_backend', 'alembic_version_rca_worker'
             )
             AND column_name = 'version_num'"""
    )
    assert dict(version_lengths) == {
        "alembic_version_backend": 64,
        "alembic_version_rca_worker": 64,
    }
    relations = await connection.fetch(
        """SELECT relname, relkind, relispartition
           FROM pg_class
           WHERE oid = ANY($1::regclass[])
           ORDER BY relname""",
        list(CANONICAL_TABLES),
    )
    assert [tuple(row) for row in relations] == [
        (table_name, b"r", False) for table_name in sorted(CANONICAL_TABLES)
    ]
    legacy_relations = await connection.fetch(
        """SELECT relname, relkind
           FROM pg_class
           WHERE oid = ANY($1::regclass[])
           ORDER BY relname""",
        [f"{table}__partitioned_legacy_0003" for table in CANONICAL_TABLES],
    )
    assert [tuple(row) for row in legacy_relations] == [
        (f"{table_name}__partitioned_legacy_0003", b"p")
        for table_name in sorted(CANONICAL_TABLES)
    ]
    primary_keys = await connection.fetch(
        """SELECT relation.relname,
                  array_agg(column_row.attname ORDER BY key_row.ordinality)
           FROM pg_constraint AS constraint_row
           JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid
           JOIN pg_namespace AS namespace
             ON namespace.oid = relation.relnamespace
           JOIN unnest(constraint_row.conkey) WITH ORDINALITY
                AS key_row(attnum, ordinality) ON true
           JOIN pg_attribute AS column_row
             ON column_row.attrelid = relation.oid
            AND column_row.attnum = key_row.attnum
           WHERE constraint_row.contype = 'p'
             AND namespace.nspname = 'public'
             AND relation.relname = ANY($1::text[])
           GROUP BY relation.relname
           ORDER BY relation.relname""",
        list(CANONICAL_TABLES),
    )
    assert [tuple(row) for row in primary_keys] == [
        (table_name, ["id"]) for table_name in sorted(CANONICAL_TABLES)
    ]
    helper_columns = await connection.fetch(
        """SELECT table_name, column_name
           FROM information_schema.columns
           WHERE table_schema = 'public'
             AND table_name = ANY($1::text[])
             AND (column_name = 'partition_timestamp'
                  OR column_name LIKE '%\\_partition_timestamp' ESCAPE '\\')""",
        [
            *CANONICAL_TABLES,
            "ingestion_dedup_keys",
            "alert_instances",
            "incident_alerts",
            "hypothesis_evidence",
        ],
    )
    assert helper_columns == []
    evidence = await connection.fetchrow(
        """SELECT
               max(CASE WHEN column_name='raw_result' THEN udt_name END) raw_type,
               max(CASE WHEN column_name='metadata' THEN udt_name END) metadata_type,
               max(CASE WHEN column_name='content_hash' THEN udt_name END) hash_type
           FROM information_schema.columns
           WHERE table_schema='public' AND table_name='evidence_records'"""
    )
    assert evidence is not None
    assert tuple(evidence) == ("bytea", "jsonb", "text")
    foreign_keys = await connection.fetch(
        """SELECT relation.relname,
                  array_agg(source_column.attname ORDER BY key_row.ordinality),
                  target_relation.relname,
                  array_agg(target_column.attname ORDER BY key_row.ordinality)
           FROM pg_constraint AS constraint_row
           JOIN pg_class AS relation ON relation.oid=constraint_row.conrelid
           JOIN pg_namespace AS source_namespace
             ON source_namespace.oid=relation.relnamespace
           JOIN pg_class AS target_relation
             ON target_relation.oid=constraint_row.confrelid
           JOIN pg_namespace AS target_namespace
             ON target_namespace.oid=target_relation.relnamespace
           JOIN unnest(constraint_row.conkey, constraint_row.confkey)
                WITH ORDINALITY AS key_row(source_num, target_num, ordinality)
             ON true
           JOIN pg_attribute AS source_column
             ON source_column.attrelid=relation.oid
            AND source_column.attnum=key_row.source_num
           JOIN pg_attribute AS target_column
             ON target_column.attrelid=target_relation.oid
            AND target_column.attnum=key_row.target_num
           WHERE target_relation.relname = ANY($1::text[])
             AND source_namespace.nspname = 'public'
             AND target_namespace.nspname = 'public'
           GROUP BY relation.relname, target_relation.relname,
                    constraint_row.conname
           ORDER BY relation.relname, target_relation.relname""",
        list(CANONICAL_TABLES),
    )
    assert all(len(row[1]) == 1 and len(row[3]) == 1 for row in foreign_keys)


async def _assert_worker_gate_fails_without_change(
    database_url: str, *, match: str
) -> None:
    connection = await _connect(database_url)
    before = await _catalog_snapshot(connection)
    worker_version_length = await connection.fetchval(
        """SELECT character_maximum_length
           FROM information_schema.columns
           WHERE table_schema='public'
             AND table_name='alembic_version_rca_worker'
             AND column_name='version_num'"""
    )
    await connection.close()
    with pytest.raises(RuntimeError, match=match):
        await asyncio.to_thread(
            upgrade_worker, database_url, "0003_validate_ordinary_runtime_tables"
        )
    connection = await _connect(database_url)
    try:
        assert await _catalog_snapshot(connection) == before
        assert (
            await connection.fetchval(
                "SELECT version_num FROM alembic_version_rca_worker"
            )
            == "0002_adk_specialist_analysis"
        )
        assert (
            await connection.fetchval(
                """SELECT character_maximum_length
                   FROM information_schema.columns
                   WHERE table_schema='public'
                     AND table_name='alembic_version_rca_worker'
                     AND column_name='version_num'"""
            )
            == worker_version_length
            == 32
        )
    finally:
        await connection.close()


def _load_worker_0003_module():
    module_path = (
        WORKER_ROOT
        / "migrations"
        / "versions"
        / "0003_validate_ordinary_runtime_tables.py"
    )
    spec = importlib.util.spec_from_file_location(
        f"worker_0003_{uuid4().hex}", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _invoke_worker_0003_body(database_url: str) -> None:
    module = _load_worker_0003_module()
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:

            def invoke(sync_connection) -> None:
                migration_context = MigrationContext.configure(sync_connection)
                with Operations.context(migration_context):
                    module.upgrade()

            await connection.run_sync(invoke)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_0003_revision_exists_after_backend_conversion() -> None:
    """Fail until Alembic can execute the named Worker validation revision."""
    async with _disposable_database() as database_url:
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


@pytest.mark.asyncio
async def test_worker_0003_requires_backend_0003_before_catalog_change() -> None:
    async with _disposable_database() as database_url:
        await asyncio.to_thread(
            upgrade_backend, database_url, "0002_grafana_normalization_v2"
        )
        await asyncio.to_thread(
            upgrade_worker, database_url, "0002_adk_specialist_analysis"
        )

        await _assert_worker_gate_fails_without_change(
            database_url,
            match="Backend 0003_non_partition_runtime_tables is required",
        )


@pytest.mark.asyncio
async def test_worker_0003_requires_worker_0002_without_replaying_it() -> None:
    async with _disposable_database() as database_url:
        await asyncio.to_thread(
            upgrade_backend, database_url, "0002_grafana_normalization_v2"
        )
        await asyncio.to_thread(
            upgrade_worker, database_url, "0002_adk_specialist_analysis"
        )
        await asyncio.to_thread(
            upgrade_backend, database_url, "0003_non_partition_runtime_tables"
        )
        await asyncio.to_thread(downgrade_worker, database_url, "0001_rca_worker_v1")
        connection = await _connect(database_url)
        before = await _catalog_snapshot(connection)
        await connection.close()

        with pytest.raises(
            RuntimeError, match="Worker 0002_adk_specialist_analysis is required"
        ):
            await _invoke_worker_0003_body(database_url)

        connection = await _connect(database_url)
        try:
            assert await _catalog_snapshot(connection) == before
            assert (
                await connection.fetchval(
                    "SELECT version_num FROM alembic_version_rca_worker"
                )
                == "0001_rca_worker_v1"
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            "ALTER TABLE evidence_records ADD COLUMN partition_timestamp TIMESTAMPTZ",
            "partition helper columns are forbidden",
        ),
        (
            """ALTER TABLE evidence_records
                   DROP CONSTRAINT evidence_records_new_pkey CASCADE,
                   ADD PRIMARY KEY (id, observed_at)""",
            "all canonical runtime tables require ordinary one-column UUID primary keys",
        ),
        (
            "ALTER TABLE evidence_records DROP CONSTRAINT ck_evidence_records_metadata_object",
            "evidence_records fidelity schema is required",
        ),
        (
            "ALTER TABLE specialist_runs DROP CONSTRAINT ck_specialist_runs_analysis_result_object",
            "specialist_runs Worker 0002 analysis schema is required",
        ),
        (
            "ALTER TABLE rca_runs DROP CONSTRAINT ck_rca_runs_failure_code",
            "Worker 0002 failure_code lifecycle schema is required",
        ),
    ),
)
async def test_worker_0003_rejects_damaged_catalog_before_version_column_change(
    mutation: str, message: str
) -> None:
    async with _disposable_database() as database_url:
        await asyncio.to_thread(
            upgrade_backend, database_url, "0002_grafana_normalization_v2"
        )
        await asyncio.to_thread(
            upgrade_worker, database_url, "0002_adk_specialist_analysis"
        )
        await asyncio.to_thread(
            upgrade_backend, database_url, "0003_non_partition_runtime_tables"
        )
        connection = await _connect(database_url)
        await connection.execute(mutation)
        await connection.close()

        await _assert_worker_gate_fails_without_change(database_url, match=message)


@pytest.mark.asyncio
async def test_worker_0003_ignores_catalog_objects_outside_public_schema() -> None:
    """A shadow schema must not participate in the public rollout gate."""
    async with _disposable_database() as database_url:
        await asyncio.to_thread(
            upgrade_backend, database_url, "0002_grafana_normalization_v2"
        )
        await asyncio.to_thread(
            upgrade_worker, database_url, "0002_adk_specialist_analysis"
        )
        await asyncio.to_thread(
            upgrade_backend, database_url, "0003_non_partition_runtime_tables"
        )
        connection = await _connect(database_url)
        await connection.execute(
            """CREATE SCHEMA task2_shadow;
               CREATE TABLE task2_shadow.evidence_records (
                   id UUID PRIMARY KEY
               );
               CREATE TABLE task2_shadow.foreign_noise (
                   evidence_id UUID NOT NULL
                       REFERENCES task2_shadow.evidence_records(id)
               )"""
        )
        await connection.close()

        await asyncio.to_thread(
            upgrade_worker,
            database_url,
            "0003_validate_ordinary_runtime_tables",
        )

        connection = await _connect(database_url)
        try:
            await _assert_final_catalog(connection)
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_worker_0003_rejects_composite_evidence_foreign_key() -> None:
    async with _disposable_database() as database_url:
        await asyncio.to_thread(
            upgrade_backend, database_url, "0002_grafana_normalization_v2"
        )
        await asyncio.to_thread(
            upgrade_worker, database_url, "0002_adk_specialist_analysis"
        )
        await asyncio.to_thread(
            upgrade_backend, database_url, "0003_non_partition_runtime_tables"
        )
        connection = await _connect(database_url)
        await connection.execute(
            """ALTER TABLE evidence_records
                   ADD COLUMN legacy_scope_id UUID NOT NULL
                       DEFAULT '00000000-0000-0000-0000-000000000001',
                   ADD CONSTRAINT uq_evidence_records_legacy_scope
                       UNIQUE (id, legacy_scope_id);
               ALTER TABLE hypothesis_evidence
                   ADD COLUMN evidence_scope_id UUID NOT NULL
                       DEFAULT '00000000-0000-0000-0000-000000000001',
                   ADD CONSTRAINT fk_hypothesis_evidence_composite_test
                       FOREIGN KEY (evidence_id, evidence_scope_id)
                       REFERENCES evidence_records (id, legacy_scope_id)"""
        )
        await connection.close()

        await _assert_worker_gate_fails_without_change(
            database_url, match="dependent foreign keys must be UUID-only"
        )


@pytest.mark.asyncio
async def test_existing_worker_head_four_gate_path_preserves_catalog_and_data() -> None:
    async with _disposable_database() as database_url:
        await asyncio.to_thread(
            upgrade_backend, database_url, "0002_grafana_normalization_v2"
        )
        await asyncio.to_thread(
            upgrade_worker, database_url, "0002_adk_specialist_analysis"
        )
        connection = await _connect(database_url)
        ids = await _seed_worker_0002_source(connection)
        assert (
            await connection.fetchval("SELECT version_num FROM alembic_version_backend")
            == "0002_grafana_normalization_v2"
        )
        assert (
            await connection.fetchval(
                "SELECT version_num FROM alembic_version_rca_worker"
            )
            == "0002_adk_specialist_analysis"
        )
        await connection.close()

        await asyncio.to_thread(
            upgrade_backend, database_url, "0003_non_partition_runtime_tables"
        )
        connection = await _connect(database_url)
        data_before_worker_gate = await _data_snapshot(connection, ids)
        catalog_before_worker_gate = await _catalog_snapshot(connection)
        await connection.close()

        await asyncio.to_thread(
            upgrade_worker,
            database_url,
            "0003_validate_ordinary_runtime_tables",
        )
        connection = await _connect(database_url)
        try:
            await _assert_final_catalog(connection)
            assert await _data_snapshot(connection, ids) == data_before_worker_gate
            assert all(
                row[2] == RAW_RESULT
                and row[3] == METADATA
                and row[4] == CONTENT_HASH
                for row in data_before_worker_gate[:4]
            )
            catalog_after_worker_gate = await _catalog_snapshot(connection)
            assert tuple(
                row
                for row in catalog_after_worker_gate
                if row[1] != "alembic_version_rca_worker"
            ) == tuple(
                row
                for row in catalog_before_worker_gate
                if row[1] != "alembic_version_rca_worker"
            )
        finally:
            await connection.close()


def test_worker_0003_downgrade_is_explicitly_unsupported() -> None:
    module = _load_worker_0003_module()

    with pytest.raises(
        RuntimeError,
        match=(
            "Worker 0003 is a forward validation gate; do not downgrade across "
            "the ordinary-table conversion"
        ),
    ):
        module.downgrade()
