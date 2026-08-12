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
async def test_delivery_token_identifier_uses_text_not_a_secret_or_uuid(connection):
    data_type = await connection.fetchval(
        """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'webhook_deliveries'
          AND column_name = 'token_id'
        """
    )

    assert data_type == "text"


@pytest.mark.asyncio
async def test_webhook_delivery_status_accepts_validation_failure_and_is_inherited(
    connection,
):
    constraint_definition = await connection.fetchval(
        """
        SELECT pg_get_constraintdef(con.oid)
        FROM pg_constraint AS con
        JOIN pg_class AS c ON c.oid = con.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'webhook_deliveries'
          AND con.contype = 'c'
          AND con.conkey = ARRAY[
              (
                  SELECT attnum
                  FROM pg_attribute
                  WHERE attrelid = c.oid AND attname = 'status'
              )
          ]::smallint[]
        """
    )
    assert constraint_definition == (
        "CHECK ((status = ANY (ARRAY['RECEIVED'::text, 'PROCESSED'::text, "
        "'DUPLICATE'::text, 'VALIDATION_FAILED'::text, 'REJECTED'::text, "
        "'FAILED'::text])))"
    )

    inherited_status_columns = await connection.fetch(
        """
        SELECT child.relname AS partition_name,
               attribute.attnotnull,
               attribute.attinhcount
        FROM pg_inherits
        JOIN pg_class AS parent ON parent.oid = inhparent
        JOIN pg_class AS child ON child.oid = inhrelid
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = child.oid
         AND attribute.attname = 'status'
        WHERE parent.oid = 'public.webhook_deliveries'::regclass
        ORDER BY child.relname
        """
    )
    assert inherited_status_columns
    assert all(row["attnotnull"] for row in inherited_status_columns)
    assert all(row["attinhcount"] == 1 for row in inherited_status_columns)


@pytest.mark.asyncio
async def test_alert_event_validation_columns_are_constrained_and_inherited(connection):
    columns = await connection.fetch(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'alert_events'
          AND column_name IN ('validation_status', 'validation_errors')
        """
    )
    assert {
        row["column_name"]: {
            "data_type": row["data_type"],
            "is_nullable": row["is_nullable"],
            "column_default": row["column_default"],
        }
        for row in columns
    } == {
        "validation_status": {
            "data_type": "text",
            "is_nullable": "NO",
            "column_default": "'VALID'::text",
        },
        "validation_errors": {
            "data_type": "jsonb",
            "is_nullable": "NO",
            "column_default": "'[]'::jsonb",
        },
    }

    constraint_definition = await connection.fetchval(
        """
        SELECT pg_get_constraintdef(con.oid)
        FROM pg_constraint AS con
        JOIN pg_class AS c ON c.oid = con.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'alert_events'
          AND con.contype = 'c'
          AND con.conkey = ARRAY[
              (
                  SELECT attnum
                  FROM pg_attribute
                  WHERE attrelid = c.oid AND attname = 'validation_status'
              )
          ]::smallint[]
        """
    )
    assert constraint_definition == (
        "CHECK ((validation_status = ANY "
        "(ARRAY['VALID'::text, 'VALIDATION_FAILED'::text])))"
    )

    inherited_columns = await connection.fetch(
        """
        SELECT child.relname AS partition_name,
               attribute.attname AS column_name,
               format_type(attribute.atttypid, attribute.atttypmod) AS data_type,
               attribute.attnotnull,
               attribute.attinhcount
        FROM pg_inherits
        JOIN pg_class AS parent ON parent.oid = inhparent
        JOIN pg_class AS child ON child.oid = inhrelid
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = child.oid
         AND attribute.attname IN ('validation_status', 'validation_errors')
        WHERE parent.oid = 'public.alert_events'::regclass
        ORDER BY child.relname, attribute.attname
        """
    )
    assert inherited_columns
    assert {
        (row["column_name"], row["data_type"], row["attnotnull"])
        for row in inherited_columns
    } == {
        ("validation_status", "text", True),
        ("validation_errors", "jsonb", True),
    }
    assert all(row["attinhcount"] == 1 for row in inherited_columns)


@pytest.mark.asyncio
async def test_incident_and_worker_job_identity_indexes_are_exact(connection):
    rows = await connection.fetch(
        """
        SELECT index_class.relname AS index_name,
               table_class.relname AS table_name,
               index_metadata.indisunique,
               array_agg(attribute.attname ORDER BY index_key.ordinality) AS columns,
               pg_get_expr(index_metadata.indpred, index_metadata.indrelid) AS predicate
        FROM pg_index AS index_metadata
        JOIN pg_class AS index_class ON index_class.oid = index_metadata.indexrelid
        JOIN pg_class AS table_class ON table_class.oid = index_metadata.indrelid
        JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
        CROSS JOIN LATERAL unnest(index_metadata.indkey)
            WITH ORDINALITY AS index_key(attnum, ordinality)
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = table_class.oid
         AND attribute.attnum = index_key.attnum
        WHERE namespace.nspname = 'public'
          AND index_class.relname IN (
              'uq_incidents_active_identity', 'uq_worker_jobs_run_type'
          )
        GROUP BY index_class.relname,
                 table_class.relname,
                 index_metadata.indisunique,
                 index_metadata.indpred,
                 index_metadata.indrelid
        """
    )
    indexes = {
        row["index_name"]: {
            "table_name": row["table_name"],
            "is_unique": row["indisunique"],
            "columns": row["columns"],
            "predicate": row["predicate"],
        }
        for row in rows
    }
    assert indexes == {
        "uq_incidents_active_identity": {
            "table_name": "incidents",
            "is_unique": True,
            "columns": ["identity_key"],
            "predicate": (
                "(status = ANY (ARRAY['OPEN'::text, 'INVESTIGATING'::text]))"
            ),
        },
        "uq_worker_jobs_run_type": {
            "table_name": "worker_jobs",
            "is_unique": True,
            "columns": ["rca_run_id", "job_type"],
            "predicate": None,
        },
    }

    identity_column = await connection.fetchrow(
        """
        SELECT data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'incidents'
          AND column_name = 'identity_key'
        """
    )
    assert identity_column is not None
    assert dict(identity_column) == {"data_type": "text", "is_nullable": "NO"}


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
