"""Acceptance coverage for the one-way runtime-table maintenance migration."""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[3]
WORKER_ROOT = BACKEND_ROOT.parent / "rca-worker"
DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent",
)
CANONICAL_TABLES = (
    "webhook_deliveries",
    "alert_events",
    "evidence_records",
    "incident_messages",
    "incident_timeline_events",
    "audit_events",
)
LEGACY_TABLES = {
    table_name: f"{table_name}__partitioned_legacy_0003"
    for table_name in CANONICAL_TABLES
}


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


def _upgrade_backend(database_url: str, revision: str) -> None:
    _upgrade(BACKEND_ROOT, database_url, revision)


def _upgrade_worker(database_url: str, revision: str) -> None:
    _upgrade(WORKER_ROOT, database_url, revision)


@dataclass
class MigrationDatabase:
    url: str
    connection: asyncpg.Connection


@pytest_asyncio.fixture
async def migration_database() -> AsyncIterator[MigrationDatabase]:
    """Start each acceptance case from a disposable, real 0002 database."""
    database_name = f"task7_migration_{uuid4().hex}"
    admin = await asyncpg.connect(
        _asyncpg_url(_with_database(DATABASE_URL, "postgres"))
    )
    test_url = _with_database(DATABASE_URL, database_name)
    connection: asyncpg.Connection | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        await asyncio.to_thread(
            _upgrade_backend, test_url, "0002_grafana_normalization_v2"
        )
        await asyncio.to_thread(
            _upgrade_worker, test_url, "0002_adk_specialist_analysis"
        )
        active_connection = await asyncpg.connect(_asyncpg_url(test_url))
        connection = active_connection
        yield MigrationDatabase(test_url, active_connection)
    finally:
        if connection is not None:
            await connection.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        await admin.close()


def _month_pair() -> tuple[datetime, datetime]:
    # 0001 creates the current and next monthly partitions.  These exact
    # timestamps exercise a cross-partition UUID migration without timezones.
    current = datetime.now(UTC).replace(
        day=1, hour=12, minute=0, second=0, microsecond=0
    )
    if current.month == 12:
        next_month = current.replace(year=current.year + 1, month=1)
    else:
        next_month = current.replace(month=current.month + 1)
    return current, next_month


async def _seed_0002_fixture(connection: asyncpg.Connection) -> dict[str, UUID]:
    ids = {
        "team": UUID("10000000-0000-0000-0000-000000000001"),
        "project": UUID("10000000-0000-0000-0000-000000000002"),
        "environment": UUID("10000000-0000-0000-0000-000000000003"),
        "service": UUID("10000000-0000-0000-0000-000000000004"),
        "source": UUID("10000000-0000-0000-0000-000000000005"),
        "subject": UUID("10000000-0000-0000-0000-000000000006"),
        "incident": UUID("10000000-0000-0000-0000-000000000007"),
        "run": UUID("10000000-0000-0000-0000-000000000008"),
        "specialist": UUID("10000000-0000-0000-0000-000000000009"),
        "hypothesis": UUID("10000000-0000-0000-0000-000000000010"),
        "delivery_one": UUID("10000000-0000-0000-0000-000000000011"),
        "delivery_two": UUID("10000000-0000-0000-0000-000000000012"),
        "event_one": UUID("10000000-0000-0000-0000-000000000013"),
        "event_two": UUID("10000000-0000-0000-0000-000000000014"),
        "evidence_one": UUID("10000000-0000-0000-0000-000000000015"),
        "evidence_two": UUID("10000000-0000-0000-0000-000000000016"),
    }
    first_month, second_month = _month_pair()
    await connection.execute(
        "INSERT INTO teams (id, name) VALUES ($1, 'task7-team')", ids["team"]
    )
    await connection.execute(
        "INSERT INTO projects (id, team_id, name) VALUES ($1, $2, 'task7-project')",
        ids["project"],
        ids["team"],
    )
    await connection.execute(
        "INSERT INTO environments (id, project_id, name) VALUES ($1, $2, 'task7-environment')",
        ids["environment"],
        ids["project"],
    )
    await connection.execute(
        "INSERT INTO services (id, environment_id, name) VALUES ($1, $2, 'task7-service')",
        ids["service"],
        ids["environment"],
    )
    await connection.execute(
        "INSERT INTO grafana_sources (id, project_id, environment_id, name) VALUES ($1, $2, $3, 'task7-source')",
        ids["source"],
        ids["project"],
        ids["environment"],
    )
    await connection.execute(
        "INSERT INTO subjects (id, external_id, subject_type) VALUES ($1, 'task7-subject', 'USER')",
        ids["subject"],
    )
    await connection.execute(
        """INSERT INTO incidents (
            id, identity_key, title, severity, status, alert_state, team_id,
            project_id, environment_id, service_id, opened_at
        ) VALUES ($1, 'task7-identity', 'task7 incident', 'SEV3', 'OPEN',
                  'FIRING', $2, $3, $4, $5, now())""",
        ids["incident"],
        ids["team"],
        ids["project"],
        ids["environment"],
        ids["service"],
    )
    await connection.execute(
        "INSERT INTO rca_runs (id, incident_id, status) VALUES ($1, $2, 'QUEUED')",
        ids["run"],
        ids["incident"],
    )
    await connection.execute(
        "INSERT INTO specialist_runs (id, rca_run_id, specialist_type, status) VALUES ($1, $2, 'METRICS', 'QUEUED')",
        ids["specialist"],
        ids["run"],
    )
    await connection.execute(
        "INSERT INTO rca_hypotheses (id, rca_run_id, statement, confidence) VALUES ($1, $2, 'task7', 0.5)",
        ids["hypothesis"],
        ids["run"],
    )
    for delivery_id, timestamp, suffix in (
        (ids["delivery_one"], first_month, "one"),
        (ids["delivery_two"], second_month, "two"),
    ):
        await connection.execute(
            """INSERT INTO webhook_deliveries (
                id, partition_timestamp, received_at, source_id, token_id,
                body_hash, raw_body, raw_payload, status
            ) VALUES ($1, $2, $2, $3, 'task7-token', $4, 'x'::bytea,
                      '{}'::jsonb, 'RECEIVED')""",
            delivery_id,
            timestamp,
            ids["source"],
            f"task7-hash-{suffix}",
        )
    for event_id, delivery_id, timestamp, suffix in (
        (ids["event_one"], ids["delivery_one"], first_month, "one"),
        (ids["event_two"], ids["delivery_two"], second_month, "two"),
    ):
        await connection.execute(
            """INSERT INTO alert_events (
                id, partition_timestamp, observed_at, source_id, delivery_id,
                delivery_partition_timestamp, fingerprint, alert_state, raw_payload
            ) VALUES ($1, $2, $2, $3, $4, $2, $5, 'FIRING', '{}'::jsonb)""",
            event_id,
            timestamp,
            ids["source"],
            delivery_id,
            f"task7-fingerprint-{suffix}",
        )
    for evidence_id, timestamp, suffix in (
        (ids["evidence_one"], first_month, "one"),
        (ids["evidence_two"], second_month, "two"),
    ):
        await connection.execute(
            """INSERT INTO evidence_records (
                id, partition_timestamp, observed_at, rca_run_id, specialist_run_id,
                evidence_type, source_agent, source_endpoint, tool_name, team_id,
                project_id, environment_id, service_id, time_window_start,
                time_window_end, structured_data, raw_result, metadata, content_hash
            ) VALUES ($1, $2, $2, $3, $4, 'METRIC', 'task7', 'endpoint', 'tool',
                      $5, $6, $7, $8, $2, $2, '{}'::jsonb, $9::bytea,
                      '{"source":"task7"}'::jsonb, $10)""",
            evidence_id,
            timestamp,
            ids["run"],
            ids["specialist"],
            ids["team"],
            ids["project"],
            ids["environment"],
            ids["service"],
            f"task7-raw-result-{suffix}".encode(),
            f"task7-content-{suffix}",
        )
    await connection.execute(
        """INSERT INTO ingestion_dedup_keys (
            source_id, dedup_key, delivery_id, delivery_partition_timestamp
        ) VALUES ($1, 'task7-dedup', $2, $3)""",
        ids["source"],
        ids["delivery_one"],
        first_month,
    )
    await connection.execute(
        """INSERT INTO alert_instances (
            source_id, fingerprint, latest_event_id, latest_event_partition_timestamp,
            state, first_seen_at, last_seen_at
        ) VALUES ($1, 'task7-instance', $2, $3, 'FIRING', $3, $3)""",
        ids["source"],
        ids["event_two"],
        second_month,
    )
    await connection.execute(
        """INSERT INTO incident_alerts (
            incident_id, alert_event_id, alert_event_partition_timestamp
        ) VALUES ($1, $2, $3)""",
        ids["incident"],
        ids["event_one"],
        first_month,
    )
    await connection.execute(
        """INSERT INTO hypothesis_evidence (
            hypothesis_id, evidence_id, evidence_partition_timestamp, relation
        ) VALUES ($1, $2, $3, 'SUPPORTS')""",
        ids["hypothesis"],
        ids["evidence_two"],
        second_month,
    )
    for timestamp, suffix in ((first_month, "one"), (second_month, "two")):
        await connection.execute(
            """INSERT INTO incident_messages (
                partition_timestamp, created_at, incident_id, role, subject_id,
                rca_run_id, content
            ) VALUES ($1, $1, $2, 'USER', $3, $4, $5)""",
            timestamp,
            ids["incident"],
            ids["subject"],
            ids["run"],
            f"task7-message-{suffix}",
        )
        await connection.execute(
            """INSERT INTO incident_timeline_events (
                partition_timestamp, occurred_at, incident_id, event_type, actor_id
            ) VALUES ($1, $1, $2, 'task7.event', $3)""",
            timestamp,
            ids["incident"],
            ids["subject"],
        )
        await connection.execute(
            """INSERT INTO audit_events (
                partition_timestamp, occurred_at, actor_id, action, resource_type
            ) VALUES ($1, $1, $2, $3, 'task7')""",
            timestamp,
            ids["subject"],
            f"task7-audit-{suffix}",
        )
    return ids


async def _upgrade_to_0003(database: MigrationDatabase) -> asyncpg.Connection:
    await database.connection.close()
    await asyncio.to_thread(
        _upgrade_backend, database.url, "0003_non_partition_runtime_tables"
    )
    return await asyncpg.connect(_asyncpg_url(database.url))


@pytest.mark.asyncio
async def test_0003_replaces_runtime_tables_and_retains_partitioned_legacy_parents(
    migration_database: MigrationDatabase,
) -> None:
    ids = await _seed_0002_fixture(migration_database.connection)
    migration_database.connection = await _upgrade_to_0003(migration_database)
    connection = migration_database.connection

    relations = await connection.fetch(
        """
        SELECT relname, relkind, relispartition
        FROM pg_class
        WHERE oid = ANY($1::regclass[])
        """,
        [*CANONICAL_TABLES, *LEGACY_TABLES.values()],
    )
    assert {
        row["relname"]: (row["relkind"], row["relispartition"])
        for row in relations
        if row["relname"] in CANONICAL_TABLES
    } == {table_name: (b"r", False) for table_name in CANONICAL_TABLES}
    assert {
        row["relname"]: (row["relkind"], row["relispartition"])
        for row in relations
        if row["relname"] in LEGACY_TABLES.values()
    } == {legacy_name: (b"p", False) for legacy_name in LEGACY_TABLES.values()}

    partition_parents = await connection.fetch(
        """
        SELECT parent.relname, partition_metadata.partstrat,
               count(child.oid) AS child_count
        FROM pg_partitioned_table AS partition_metadata
        JOIN pg_class AS parent ON parent.oid = partition_metadata.partrelid
        LEFT JOIN pg_inherits AS inheritance ON inheritance.inhparent = parent.oid
        LEFT JOIN pg_class AS child ON child.oid = inheritance.inhrelid
        WHERE parent.relname = ANY($1::text[])
        GROUP BY parent.relname, partition_metadata.partstrat
        """,
        list(LEGACY_TABLES.values()),
    )
    assert {
        row["relname"]: (row["partstrat"], row["child_count"])
        for row in partition_parents
    } == {legacy_name: (b"r", 2) for legacy_name in LEGACY_TABLES.values()}

    primary_keys = await connection.fetch(
        """
        SELECT relation.relname, array_agg(attribute.attname ORDER BY key.ordinality) AS columns
        FROM pg_constraint AS con
        JOIN pg_class AS relation ON relation.oid = con.conrelid
        CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS key(attnum, ordinality)
        JOIN pg_attribute AS attribute ON attribute.attrelid = relation.oid AND attribute.attnum = key.attnum
        WHERE con.contype = 'p' AND relation.relname = ANY($1::text[])
        GROUP BY relation.relname
        """,
        list(CANONICAL_TABLES),
    )
    assert {row["relname"]: row["columns"] for row in primary_keys} == {
        table_name: ["id"] for table_name in CANONICAL_TABLES
    }

    for table_name in CANONICAL_TABLES:
        expected_count = 2
        assert (
            await connection.fetchval(f"SELECT count(*) FROM {table_name}")
            == expected_count
        )
        assert (
            await connection.fetchval(
                f"SELECT count(*) FROM {LEGACY_TABLES[table_name]}"
            )
            == expected_count
        )
    for table_name in (
        "ingestion_dedup_keys",
        "alert_instances",
        "incident_alerts",
        "hypothesis_evidence",
    ):
        assert await connection.fetchval(f"SELECT count(*) FROM {table_name}") == 1

    forbidden_columns = await connection.fetch(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = ANY($1::text[])
          AND (column_name = 'partition_timestamp' OR column_name LIKE '%_partition_timestamp')
        """,
        [
            *CANONICAL_TABLES,
            "ingestion_dedup_keys",
            "alert_instances",
            "incident_alerts",
            "hypothesis_evidence",
        ],
    )
    assert not forbidden_columns

    foreign_keys = await connection.fetch(
        """
        SELECT source.relname AS source_table, target.relname AS target_table,
               array_length(con.conkey, 1) AS source_columns,
               array_length(con.confkey, 1) AS target_columns
        FROM pg_constraint AS con
        JOIN pg_class AS source ON source.oid = con.conrelid
        JOIN pg_class AS target ON target.oid = con.confrelid
        WHERE con.contype = 'f'
        """
    )
    fk_shapes = {
        (
            row["source_table"],
            row["target_table"],
            row["source_columns"],
            row["target_columns"],
        )
        for row in foreign_keys
    }
    assert {
        ("alert_events", "webhook_deliveries", 1, 1),
        ("ingestion_dedup_keys", "webhook_deliveries", 1, 1),
        ("alert_instances", "alert_events", 1, 1),
        ("incident_alerts", "alert_events", 1, 1),
        ("hypothesis_evidence", "evidence_records", 1, 1),
    } <= fk_shapes
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await connection.execute(
            "INSERT INTO ingestion_dedup_keys (source_id, dedup_key, delivery_id) VALUES ($1, 'task7-invalid', $2)",
            ids["source"],
            uuid4(),
        )
    await connection.execute(
        "INSERT INTO audit_events (action, resource_type) VALUES ('task7-write', 'task7')"
    )
    assert await connection.fetchval("SELECT count(*) FROM audit_events") == 3
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM audit_events__partitioned_legacy_0003"
        )
        == 2
    )


@pytest.mark.asyncio
async def test_0003_rejects_duplicate_uuids_before_replacement(
    migration_database: MigrationDatabase,
) -> None:
    ids = await _seed_0002_fixture(migration_database.connection)
    _, second_month = _month_pair()
    await migration_database.connection.execute(
        """INSERT INTO webhook_deliveries (
            id, partition_timestamp, received_at, source_id, body_hash, raw_body,
            raw_payload, status
        ) VALUES ($1, $2, $2, $3, 'task7-duplicate', 'x'::bytea, '{}'::jsonb, 'RECEIVED')""",
        ids["delivery_one"],
        second_month,
        ids["source"],
    )
    await migration_database.connection.close()
    with pytest.raises(Exception, match="duplicate UUID precheck failed"):
        await asyncio.to_thread(
            _upgrade_backend,
            migration_database.url,
            "0003_non_partition_runtime_tables",
        )
    migration_database.connection = await asyncpg.connect(
        _asyncpg_url(migration_database.url)
    )
    assert (
        await migration_database.connection.fetchval(
            "SELECT to_regclass('public.webhook_deliveries_new')"
        )
        is None
    )
    assert (
        await migration_database.connection.fetchval(
            "SELECT version_num FROM alembic_version_backend"
        )
        == "0002_grafana_normalization_v2"
    )
