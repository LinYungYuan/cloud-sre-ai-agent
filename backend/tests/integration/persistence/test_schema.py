import os
from datetime import UTC, date, datetime
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

from sre_agent.persistence.database import ensure_monthly_partitions

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql://postgres@127.0.0.1:55432/sre_agent",
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

TEAM_A = UUID("91000000-0000-0000-0000-000000000001")
TEAM_B = UUID("91000000-0000-0000-0000-000000000002")
PROJECT_A = UUID("92000000-0000-0000-0000-000000000001")
PROJECT_B = UUID("92000000-0000-0000-0000-000000000002")
ENVIRONMENT_A = UUID("93000000-0000-0000-0000-000000000001")
ENVIRONMENT_B = UUID("93000000-0000-0000-0000-000000000002")
SERVICE_A = UUID("94000000-0000-0000-0000-000000000001")
SERVICE_B = UUID("94000000-0000-0000-0000-000000000002")


@pytest_asyncio.fixture
async def connection():
    connection = await asyncpg.connect(DATABASE_URL)
    try:
        yield connection
    finally:
        await connection.close()


async def _insert_hierarchy_fixture(connection) -> None:
    await connection.executemany(
        "INSERT INTO teams (id, name) VALUES ($1, $2)",
        [(TEAM_A, "hierarchy-team-a"), (TEAM_B, "hierarchy-team-b")],
    )
    await connection.executemany(
        "INSERT INTO projects (id, team_id, name) VALUES ($1, $2, $3)",
        [
            (PROJECT_A, TEAM_A, "hierarchy-project-a"),
            (PROJECT_B, TEAM_B, "hierarchy-project-b"),
        ],
    )
    await connection.executemany(
        "INSERT INTO environments (id, project_id, name) VALUES ($1, $2, $3)",
        [
            (ENVIRONMENT_A, PROJECT_A, "hierarchy-environment-a"),
            (ENVIRONMENT_B, PROJECT_B, "hierarchy-environment-b"),
        ],
    )
    await connection.executemany(
        "INSERT INTO services (id, environment_id, name) VALUES ($1, $2, $3)",
        [
            (SERVICE_A, ENVIRONMENT_A, "hierarchy-service-a"),
            (SERVICE_B, ENVIRONMENT_B, "hierarchy-service-b"),
        ],
    )


async def _assert_integrity_violation(connection, statement: str, *values) -> None:
    savepoint = connection.transaction()
    await savepoint.start()
    try:
        with pytest.raises(asyncpg.IntegrityConstraintViolationError):
            await connection.execute(statement, *values)
    finally:
        await savepoint.rollback()


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
    assert {
        name: b"r" for name in PARTITIONED_TABLES
    }.items() <= partition_strategies.items()

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
    current_month = datetime.now(UTC).date().replace(day=1)
    next_month = (
        date(current_month.year + 1, 1, 1)
        if current_month.month == 12
        else date(current_month.year, current_month.month + 1, 1)
    )
    current_partition = (
        f"alert_events_{current_month.year:04d}_{current_month.month:02d}"
    )
    next_partition = f"alert_events_{next_month.year:04d}_{next_month.month:02d}"
    inherited_by_partition = {
        partition_name: {
            row["column_name"]: {
                "data_type": row["data_type"],
                "not_null": row["attnotnull"],
                "inheritance_count": row["attinhcount"],
            }
            for row in inherited_columns
            if row["partition_name"] == partition_name
        }
        for partition_name in (current_partition, next_partition)
    }
    expected_columns = {
        "validation_errors": {
            "data_type": "jsonb",
            "not_null": True,
            "inheritance_count": 1,
        },
        "validation_status": {
            "data_type": "text",
            "not_null": True,
            "inheritance_count": 1,
        },
    }
    assert inherited_by_partition[current_partition] == expected_columns
    assert inherited_by_partition[next_partition] == expected_columns


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
        table_name: ["id", "partition_timestamp"] for table_name in PARTITIONED_TABLES
    }


@pytest.mark.asyncio
async def test_grafana_source_rejects_environment_from_another_project(connection):
    transaction = connection.transaction()
    await transaction.start()
    try:
        await _insert_hierarchy_fixture(connection)

        await _assert_integrity_violation(
            connection,
            """
            INSERT INTO grafana_sources (project_id, environment_id, name)
            VALUES ($1, $2, 'cross-branch-source')
            """,
            PROJECT_A,
            ENVIRONMENT_B,
        )
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
async def test_incident_rejects_each_cross_branch_scope_pair_and_allows_null_service(
    connection,
):
    transaction = connection.transaction()
    await transaction.start()
    try:
        await _insert_hierarchy_fixture(connection)
        statement = """
            INSERT INTO incidents (
                id, identity_key, title, severity, status, alert_state,
                team_id, project_id, environment_id, service_id, opened_at
            ) VALUES ($1, $2, 'Hierarchy test', 'SEV3', 'OPEN', 'FIRING',
                      $3, $4, $5, $6, now())
        """
        invalid_scopes = (
            (TEAM_B, PROJECT_A, ENVIRONMENT_A, SERVICE_A),
            (TEAM_A, PROJECT_B, ENVIRONMENT_A, SERVICE_A),
            (TEAM_A, PROJECT_A, ENVIRONMENT_B, SERVICE_A),
            (TEAM_A, PROJECT_A, ENVIRONMENT_A, SERVICE_B),
        )
        for offset, scope in enumerate(invalid_scopes, start=1):
            await _assert_integrity_violation(
                connection,
                statement,
                UUID(f"95000000-0000-0000-0000-{offset:012d}"),
                f"cross-branch-incident-{offset}",
                *scope,
            )

        await connection.execute(
            statement,
            UUID("95000000-0000-0000-0000-000000000099"),
            "valid-null-service-incident",
            TEAM_A,
            PROJECT_A,
            ENVIRONMENT_A,
            None,
        )
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
async def test_nullable_scope_tables_reject_gaps_and_cross_branch_pairs(connection):
    transaction = connection.transaction()
    await transaction.start()
    try:
        await _insert_hierarchy_fixture(connection)
        mapping_statement = """
            INSERT INTO classification_mappings (
                matcher, team_id, project_id, environment_id, service_id
            ) VALUES ('{}'::jsonb, $1, $2, $3, $4)
        """
        for scope in (
            (TEAM_A, PROJECT_B, None, None),
            (TEAM_A, None, ENVIRONMENT_B, None),
            (None, PROJECT_A, None, SERVICE_B),
            (None, None, ENVIRONMENT_A, SERVICE_B),
        ):
            await _assert_integrity_violation(
                connection,
                mapping_statement,
                *scope,
            )

        await connection.execute(
            mapping_statement,
            None,
            None,
            None,
            SERVICE_A,
        )

        incident_id = UUID("95000000-0000-0000-0000-000000000100")
        run_id = UUID("96000000-0000-0000-0000-000000000100")
        await connection.execute(
            """
            INSERT INTO incidents (
                id, identity_key, title, severity, status, alert_state,
                team_id, project_id, environment_id, service_id, opened_at
            ) VALUES ($1, 'evidence-parent', 'Evidence parent', 'SEV3', 'OPEN',
                      'FIRING', $2, $3, $4, $5, now())
            """,
            incident_id,
            TEAM_A,
            PROJECT_A,
            ENVIRONMENT_A,
            SERVICE_A,
        )
        await connection.execute(
            "INSERT INTO rca_runs (id, incident_id, status) VALUES ($1, $2, 'QUEUED')",
            run_id,
            incident_id,
        )
        evidence_statement = """
            INSERT INTO evidence_records (
                id, partition_timestamp, observed_at, rca_run_id,
                evidence_type, source_agent, source_endpoint, tool_name,
                team_id, project_id, environment_id, service_id,
                time_window_start, time_window_end, structured_data,
                raw_result_reference, content_hash
            ) VALUES ($1, now(), now(), $2, 'metric', 'test', '/test', 'query',
                      $3, $4, $5, $6, now(), now(), '{}'::jsonb, 'ref', 'hash')
        """
        for offset, scope in enumerate(
            (
                (TEAM_A, PROJECT_B, None, None),
                (TEAM_A, None, ENVIRONMENT_B, None),
                (None, PROJECT_A, None, SERVICE_B),
                (None, None, ENVIRONMENT_A, SERVICE_B),
            ),
            start=1,
        ):
            await _assert_integrity_violation(
                connection,
                evidence_statement,
                UUID(f"97000000-0000-0000-0000-{offset:012d}"),
                run_id,
                *scope,
            )

        await connection.execute(
            evidence_statement,
            UUID("97000000-0000-0000-0000-000000000099"),
            run_id,
            None,
            PROJECT_A,
            None,
            None,
        )
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
async def test_required_uniqueness_indexes_exist(connection):
    rows = await connection.fetch(
        """
        SELECT table_class.relname AS table_name,
               index_class.relname AS index_name,
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
          AND table_class.relname = ANY($1::text[])
          AND index_metadata.indisunique
        GROUP BY table_class.relname,
                 index_class.relname,
                 index_metadata.indisunique,
                 index_metadata.indpred,
                 index_metadata.indrelid
        """,
        ["rca_runs", "ingestion_dedup_keys", "alert_instances", "outbox_events"],
    )
    definitions = [
        {
            "table_name": row["table_name"],
            "index_name": row["index_name"],
            "columns": row["columns"],
            "predicate": row["predicate"],
        }
        for row in rows
    ]

    def matching(
        table_name: str,
        columns: list[str],
        predicate: str | None,
    ) -> list[dict[str, object]]:
        return [
            definition
            for definition in definitions
            if definition["table_name"] == table_name
            and definition["columns"] == columns
            and definition["predicate"] == predicate
        ]

    assert len(matching("ingestion_dedup_keys", ["source_id", "dedup_key"], None)) == 1
    assert len(matching("alert_instances", ["source_id", "fingerprint"], None)) == 1
    assert len(matching("outbox_events", ["idempotency_key"], None)) == 1
    active_run_predicate = (
        "(status = ANY (ARRAY['WAITING_FOR_CLASSIFICATION'::text, "
        "'QUEUED'::text, 'RUNNING'::text]))"
    )
    assert len(matching("rca_runs", ["incident_id"], active_run_predicate)) == 1


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


@pytest.mark.asyncio
async def test_partition_maintenance_rejects_same_name_ordinary_table(connection):
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute(
            "CREATE TABLE public.alert_events_2035_01 (placeholder integer)"
        )

        with pytest.raises(RuntimeError, match="partition drift"):
            await ensure_monthly_partitions(connection, date(2035, 1, 1))
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
async def test_partition_maintenance_rejects_partition_attached_to_wrong_parent(
    connection,
):
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute(
            """
            CREATE TABLE public.alert_events_2035_02
            PARTITION OF public.audit_events
            FOR VALUES FROM ('2035-02-01 00:00:00+00')
                         TO ('2035-03-01 00:00:00+00')
            """
        )

        with pytest.raises(RuntimeError, match="partition drift"):
            await ensure_monthly_partitions(connection, date(2035, 2, 1))
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
async def test_partition_maintenance_rejects_wrong_bounds_on_expected_parent(
    connection,
):
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute(
            """
            CREATE TABLE public.alert_events_2035_03
            PARTITION OF public.alert_events
            FOR VALUES FROM ('2035-03-02 00:00:00+00')
                         TO ('2035-04-01 00:00:00+00')
            """
        )

        with pytest.raises(RuntimeError, match="partition drift"):
            await ensure_monthly_partitions(connection, date(2035, 3, 1))
    finally:
        await transaction.rollback()
