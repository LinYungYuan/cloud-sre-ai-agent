import os
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql://postgres@127.0.0.1:55432/sre_agent",
).replace("postgresql+asyncpg://", "postgresql://", 1)

RUNTIME_TABLES = {
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
    "normalization_rules",
    "folder_scope_mappings",
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
async def test_migration_creates_approved_tables_and_ordinary_runtime_relations(
    connection,
):
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
        SELECT c.relname, c.relkind, c.relispartition
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])
        """,
        list(RUNTIME_TABLES),
    )
    assert {
        row["relname"]: (row["relkind"], row["relispartition"]) for row in rows
    } == {name: (b"r", False) for name in RUNTIME_TABLES}

    incident_kind = await connection.fetchval(
        "SELECT relkind FROM pg_class WHERE oid = 'public.incidents'::regclass"
    )
    assert incident_kind == b"r"


@pytest.mark.asyncio
async def test_lifecycle_timestamps_are_timezone_aware(connection):
    rows = await connection.fetch(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND (
            column_name LIKE '%_at'
            OR column_name IN ('time_window_start', 'time_window_end')
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
async def test_normalization_v2_columns_and_nullable_incident_scope(connection):
    rows = await connection.fetch(
        """
        SELECT table_name, column_name, data_type, udt_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND (
            (table_name = 'webhook_deliveries'
             AND column_name IN ('truncated_alerts', 'incomplete'))
            OR
            (table_name = 'alert_events'
             AND column_name IN (
               'provider', 'folder_code', 'alert_name', 'severity_raw',
               'severity_canonical', 'issue', 'resource', 'normalization_status',
               'normalization_rule_id', 'normalization_rule_version',
               'normalization_warnings'
             ))
            OR
            (table_name = 'incidents'
             AND column_name IN (
               'identity_version', 'provider', 'folder_code', 'alert_name',
               'team_id', 'project_id', 'environment_id', 'service_id'
             ))
          )
        """
    )
    columns = {(row["table_name"], row["column_name"]): row for row in rows}

    assert columns[("webhook_deliveries", "truncated_alerts")]["column_default"] == "0"
    assert columns[("webhook_deliveries", "incomplete")]["column_default"] == "false"
    assert columns[("alert_events", "provider")]["data_type"] == "text"
    assert columns[("alert_events", "issue")]["udt_name"] == "jsonb"
    assert columns[("alert_events", "normalization_warnings")]["udt_name"] == "jsonb"
    assert columns[("incidents", "identity_version")]["column_default"] == "1"
    for scope_column in ("team_id", "project_id", "environment_id", "service_id"):
        assert columns[("incidents", scope_column)]["is_nullable"] == "YES"


@pytest.mark.asyncio
async def test_normalization_catalog_constraints_and_backend_version_table(connection):
    tables = set(
        await connection.fetchval(
            """
            SELECT array_agg(tablename)
            FROM pg_tables
            WHERE schemaname = 'public'
            """
        )
        or []
    )
    assert {"normalization_rules", "folder_scope_mappings"} <= tables
    assert "alembic_version_backend" in tables
    assert "alembic_version" not in tables

    definitions = await connection.fetch(
        """
        SELECT c.relname, con.conname, con.contype, pg_get_constraintdef(con.oid) AS definition
        FROM pg_constraint AS con
        JOIN pg_class AS c ON c.oid = con.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname IN ('normalization_rules', 'folder_scope_mappings', 'incidents')
        """
    )
    constraints = {
        (row["relname"], row["conname"]): (row["contype"], row["definition"])
        for row in definitions
    }
    assert (
        constraints[
            ("normalization_rules", "uq_normalization_rules_source_name_version")
        ][0]
        == b"u"
    )
    assert (
        "NULLS NOT DISTINCT"
        in constraints[
            ("normalization_rules", "uq_normalization_rules_source_name_version")
        ][1]
    )
    assert (
        constraints[("folder_scope_mappings", "uq_folder_scope_source_folder")][0]
        == b"u"
    )
    assert (
        "identity_version = ANY"
        in constraints[("incidents", "ck_incidents_identity_version")][1]
    )
    assert "SEV1" in constraints[("incidents", "ck_incidents_severity_v2")][1]
    assert "UNMAPPED" in constraints[("incidents", "ck_incidents_severity_v2")][1]


@pytest.mark.asyncio
async def test_webhook_delivery_status_accepts_validation_failure(
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


@pytest.mark.asyncio
async def test_alert_event_validation_columns_are_constrained(connection):
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
            "columns": ["identity_version", "identity_key"],
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
async def test_runtime_tables_use_uuid_primary_keys_and_uuid_only_references(
    connection,
):
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
        list(RUNTIME_TABLES),
    )
    assert {row["relname"]: row["columns"] for row in rows} == {
        table_name: ["id"] for table_name in RUNTIME_TABLES
    }

    helper_columns = await connection.fetch(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = ANY($1::text[])
          AND (column_name = 'partition_timestamp'
               OR column_name LIKE '%_partition_timestamp')
        """,
        [
            *RUNTIME_TABLES,
            "ingestion_dedup_keys",
            "alert_instances",
            "incident_alerts",
            "hypothesis_evidence",
        ],
    )
    assert not helper_columns

    foreign_keys = await connection.fetch(
        """
        SELECT source.relname AS source_table,
               target.relname AS target_table,
               array_length(con.conkey, 1) AS source_key_count,
               array_length(con.confkey, 1) AS target_key_count
        FROM pg_constraint AS con
        JOIN pg_class AS source ON source.oid = con.conrelid
        JOIN pg_class AS target ON target.oid = con.confrelid
        WHERE con.contype = 'f'
        """
    )
    assert {
        ("alert_events", "webhook_deliveries", 1, 1),
        ("ingestion_dedup_keys", "webhook_deliveries", 1, 1),
        ("alert_instances", "alert_events", 1, 1),
        ("incident_alerts", "alert_events", 1, 1),
        ("hypothesis_evidence", "evidence_records", 1, 1),
    } <= {
        (
            row["source_table"],
            row["target_table"],
            row["source_key_count"],
            row["target_key_count"],
        )
        for row in foreign_keys
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
async def test_runtime_table_business_indexes_are_preserved(connection):
    rows = await connection.fetch(
        """
        SELECT index_class.relname AS index_name,
               table_class.relname AS table_name,
               array_agg(attribute.attname ORDER BY index_key.ordinality) AS columns
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
          AND index_class.relname = ANY($1::text[])
        GROUP BY index_class.relname, table_class.relname, index_metadata.indexrelid
        """,
        [
            "ix_webhook_deliveries_source_received",
            "ix_alert_events_source_fingerprint_observed",
            "ix_alert_instances_state_last_seen",
            "ix_evidence_records_run_observed",
            "ix_incident_messages_incident_created",
            "ix_incident_timeline_incident_occurred",
            "ix_audit_events_resource_occurred",
        ],
    )
    assert {row["index_name"]: (row["table_name"], row["columns"]) for row in rows} == {
        "ix_webhook_deliveries_source_received": (
            "webhook_deliveries",
            ["source_id", "received_at"],
        ),
        "ix_alert_events_source_fingerprint_observed": (
            "alert_events",
            ["source_id", "fingerprint", "observed_at"],
        ),
        "ix_alert_instances_state_last_seen": (
            "alert_instances",
            ["state", "last_seen_at"],
        ),
        "ix_evidence_records_run_observed": (
            "evidence_records",
            ["rca_run_id", "observed_at"],
        ),
        "ix_incident_messages_incident_created": (
            "incident_messages",
            ["incident_id", "created_at"],
        ),
        "ix_incident_timeline_incident_occurred": (
            "incident_timeline_events",
            ["incident_id", "occurred_at"],
        ),
        "ix_audit_events_resource_occurred": (
            "audit_events",
            ["resource_type", "resource_id", "occurred_at"],
        ),
    }
