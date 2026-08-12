import os
from datetime import UTC, date, datetime

import asyncpg
import pytest
import pytest_asyncio

from sre_agent.persistence.database import ensure_monthly_partitions

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:55432/sre_agent",
).replace("postgresql+asyncpg://", "postgresql://", 1)

PARTITIONED_TABLES = {
    "webhook_deliveries",
    "alert_events",
    "evidence_records",
    "incident_messages",
    "incident_timeline_events",
    "audit_events",
}

REQUIRED_TABLES = {
    "teams",
    "projects",
    "environments",
    "services",
    "subjects",
    "scope_grants",
    "grafana_sources",
    "webhook_deliveries",
    "ingestion_dedup_keys",
    "alert_events",
    "alert_instances",
    "classification_mappings",
    "incidents",
    "incident_alerts",
    "incident_assignments",
    "incident_status_history",
    "rca_runs",
    "specialist_runs",
    "evidence_records",
    "rca_hypotheses",
    "hypothesis_evidence",
    "rca_reports",
    "incident_messages",
    "incident_timeline_events",
    "audit_events",
    "outbox_events",
    "worker_jobs",
    "worker_attempts",
}


@pytest_asyncio.fixture
async def connection():
    connection = await asyncpg.connect(DATABASE_URL)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_migration_creates_approved_tables_and_range_partitions(connection):
    tables = set(
        await connection.fetchval(
            """
            SELECT array_agg(tablename ORDER BY tablename)
            FROM pg_tables
            WHERE schemaname = 'public'
            """
        )
        or []
    )
    assert REQUIRED_TABLES <= tables

    rows = await connection.fetch(
        """
        SELECT c.relname, p.partstrat
        FROM pg_partitioned_table AS p
        JOIN pg_class AS c ON c.oid = p.partrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
        """
    )
    partition_strategies = {row["relname"]: row["partstrat"] for row in rows}
    assert partition_strategies == {name: b"r" for name in PARTITIONED_TABLES}

    incident_kind = await connection.fetchval(
        "SELECT relkind FROM pg_class WHERE oid = 'public.incidents'::regclass"
    )
    assert incident_kind == b"r"

    current_month = datetime.now(UTC).date().replace(day=1)
    next_month = (
        date(current_month.year + 1, 1, 1)
        if current_month.month == 12
        else date(current_month.year, current_month.month + 1, 1)
    )
    expected_partitions = {
        f"{table_name}_{month.year:04d}_{month.month:02d}"
        for table_name in PARTITIONED_TABLES
        for month in (current_month, next_month)
    }
    actual_partitions = set(
        await connection.fetchval(
            """
            SELECT array_agg(child.relname)
            FROM pg_inherits
            JOIN pg_class AS child ON child.oid = inhrelid
            JOIN pg_class AS parent ON parent.oid = inhparent
            WHERE parent.relname = ANY($1::text[])
            """,
            list(PARTITIONED_TABLES),
        )
        or []
    )
    assert expected_partitions <= actual_partitions


@pytest.mark.asyncio
async def test_lifecycle_timestamps_are_timezone_aware(connection):
    rows = await connection.fetch(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND (
            column_name LIKE '%_at'
            OR column_name IN ('partition_timestamp', 'time_window_start', 'time_window_end')
          )
        """
    )
    assert rows
    assert {
        (row["table_name"], row["column_name"], row["data_type"]) for row in rows
    } == {
        (row["table_name"], row["column_name"], "timestamp with time zone")
        for row in rows
    }

    raw_payload_columns = await connection.fetch(
        """
        SELECT table_name, udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name IN ('webhook_deliveries', 'alert_events')
          AND column_name = 'raw_payload'
        """
    )
    assert {(row["table_name"], row["udt_name"]) for row in raw_payload_columns} == {
        ("webhook_deliveries", "jsonb"),
        ("alert_events", "jsonb"),
    }


@pytest.mark.asyncio
async def test_partitioned_tables_use_composite_logical_and_partition_key(connection):
    rows = await connection.fetch(
        """
        SELECT c.relname, array_agg(a.attname ORDER BY key.ordinality) AS columns
        FROM pg_constraint AS con
        JOIN pg_class AS c ON c.oid = con.conrelid
        CROSS JOIN LATERAL unnest(con.conkey)
            WITH ORDINALITY AS key(attnum, ordinality)
        JOIN pg_attribute AS a ON a.attrelid = c.oid AND a.attnum = key.attnum
        WHERE con.contype = 'p' AND c.relname = ANY($1::text[])
        GROUP BY c.relname
        """,
        list(PARTITIONED_TABLES),
    )
    assert {row["relname"]: row["columns"] for row in rows} == {
        table_name: ["id", "partition_timestamp"]
        for table_name in PARTITIONED_TABLES
    }


@pytest.mark.asyncio
async def test_required_uniqueness_indexes_exist(connection):
    rows = await connection.fetch(
        """
        SELECT tablename, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename IN (
            'rca_runs', 'ingestion_dedup_keys', 'alert_instances', 'outbox_events'
          )
        """
    )
    definitions = "\n".join(row["indexdef"] for row in rows)
    assert "UNIQUE" in definitions
    assert "(source_id, dedup_key)" in definitions
    assert "(source_id, fingerprint)" in definitions
    assert "(idempotency_key)" in definitions
    assert "WHERE (status = ANY" in definitions
    assert "WAITING_FOR_CLASSIFICATION" in definitions
    assert "QUEUED" in definitions
    assert "RUNNING" in definitions


@pytest.mark.asyncio
async def test_ensure_monthly_partitions_is_idempotent_with_exclusive_upper_bound(
    connection,
):
    await ensure_monthly_partitions(connection, date(2031, 12, 1))
    await ensure_monthly_partitions(connection, date(2031, 12, 17))

    for table_name in PARTITIONED_TABLES:
        partition_name = f"{table_name}_2031_12"
        bound = await connection.fetchval(
            """
            SELECT pg_get_expr(c.relpartbound, c.oid)
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = $1
            """,
            partition_name,
        )
        assert bound == (
            "FOR VALUES FROM ('2031-12-01 00:00:00+00') TO ('2032-01-01 00:00:00+00')"
        )
