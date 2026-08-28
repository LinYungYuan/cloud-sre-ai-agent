"""Validate the ordinary UUID-keyed runtime schema after Backend conversion."""

import re
from collections.abc import Sequence

from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0003_validate_ordinary_runtime_tables"
down_revision: str | None = "0002_adk_specialist_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKEND_REQUIRED_REVISION = "0003_non_partition_runtime_tables"
WORKER_REQUIRED_REVISION = "0002_adk_specialist_analysis"
CANONICAL_TABLES = (
    "webhook_deliveries",
    "alert_events",
    "evidence_records",
    "incident_messages",
    "incident_timeline_events",
    "audit_events",
)
DEPENDENT_TABLES = (
    "ingestion_dedup_keys",
    "alert_instances",
    "incident_alerts",
    "hypothesis_evidence",
)
WORKER_FAILURE_CODES = {
    "DEADLINE_EXCEEDED",
    "MCP_TIMEOUT",
    "MCP_TRANSPORT",
    "POLICY_DENIED",
    "VALIDATION_FAILED",
    "INTERNAL_ERROR",
    "NO_SAFE_MCP_CAPABILITY",
    "MCP_PAYLOAD_TOO_LARGE",
    "MCP_RESULT_INVALID",
    "ANALYSIS_TIMEOUT",
    "ANALYSIS_SCHEMA_INVALID",
    "ANALYSIS_UNKNOWN_EVIDENCE",
    "ANALYSIS_INPUT_TRUNCATED",
    "ANALYSIS_FAILED",
}
WORKER_SPECIALIST_STATUSES = {
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "SKIPPED",
    "PARTIAL",
}


def _require_version(
    connection: Connection, table_name: str, required_revision: str, owner: str
) -> None:
    version_table = connection.exec_driver_sql(
        "SELECT to_regclass($1)", (f"public.{table_name}",)
    ).scalar_one()
    revision = (
        connection.exec_driver_sql(
            f"SELECT version_num FROM public.{table_name}"
        ).scalar_one_or_none()
        if version_table is not None
        else None
    )
    if revision != required_revision:
        raise RuntimeError(f"{owner} {required_revision} is required")


def _column_catalog(
    connection: Connection, table_name: str
) -> dict[str, tuple[str, str]]:
    rows = connection.exec_driver_sql(
        """SELECT column_name, udt_name, is_nullable
           FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = $1""",
        (table_name,),
    )
    return {
        column_name: (data_type, is_nullable)
        for column_name, data_type, is_nullable in rows
    }


def _check_definition(
    connection: Connection, table_name: str, constraint_name: str
) -> str | None:
    return connection.exec_driver_sql(
        """SELECT pg_get_constraintdef(constraint_row.oid, true)
           FROM pg_constraint AS constraint_row
           WHERE constraint_row.contype = 'c'
             AND constraint_row.conrelid = to_regclass($1)
             AND constraint_row.conname = $2""",
        (f"public.{table_name}", constraint_name),
    ).scalar_one_or_none()


def _sql_literals(definition: str) -> set[str]:
    return {
        literal.replace("''", "'")
        for literal in re.findall(r"'((?:''|[^'])*)'(?:\s*::text)?", definition)
    }


def _has_required_check(
    connection: Connection,
    table_name: str,
    constraint_name: str,
    *,
    column_name: str,
    literals: set[str],
    fragments: tuple[str, ...] = (),
) -> bool:
    definition = _check_definition(connection, table_name, constraint_name)
    if definition is None or _sql_literals(definition) != literals:
        return False
    normalized = re.sub(r"\s+", " ", definition).lower()
    return column_name.lower() in normalized and all(
        fragment.lower() in normalized for fragment in fragments
    )


def _has_index(
    connection: Connection, index_name: str, *, fragments: tuple[str, ...]
) -> bool:
    definition = connection.exec_driver_sql(
        """SELECT pg_get_indexdef(index_relation.oid)
           FROM pg_class AS index_relation
           JOIN pg_namespace AS namespace
             ON namespace.oid = index_relation.relnamespace
           WHERE namespace.nspname = 'public'
             AND index_relation.relkind = 'i'
             AND index_relation.relname = $1""",
        (index_name,),
    ).scalar_one_or_none()
    if definition is None:
        return False
    normalized = re.sub(r"\s+", " ", definition).lower()
    return all(fragment.lower() in normalized for fragment in fragments)


def _require_ordinary_uuid_primary_keys(connection: Connection) -> None:
    rows = connection.exec_driver_sql(
        """SELECT relation.relname, relation.relkind::text,
                  relation.relispartition,
                  array_agg(column_row.attname ORDER BY key_row.ordinality),
                  array_agg(type_row.typname ORDER BY key_row.ordinality)
           FROM pg_class AS relation
           JOIN pg_namespace AS namespace
             ON namespace.oid = relation.relnamespace
           LEFT JOIN pg_constraint AS constraint_row
             ON constraint_row.conrelid = relation.oid
            AND constraint_row.contype = 'p'
           LEFT JOIN unnest(constraint_row.conkey) WITH ORDINALITY
                AS key_row(attnum, ordinality) ON true
           LEFT JOIN pg_attribute AS column_row
             ON column_row.attrelid = relation.oid
            AND column_row.attnum = key_row.attnum
           LEFT JOIN pg_type AS type_row ON type_row.oid = column_row.atttypid
           WHERE namespace.nspname = 'public'
             AND relation.relname = ANY($1::text[])
           GROUP BY relation.relname, relation.relkind,
                    relation.relispartition
           ORDER BY relation.relname""",
        (list(CANONICAL_TABLES),),
    )
    expected = {
        table_name: ("r", False, ["id"], ["uuid"]) for table_name in CANONICAL_TABLES
    }
    actual = {
        table_name: (relation_kind, is_partition, key_columns, key_types)
        for (
            table_name,
            relation_kind,
            is_partition,
            key_columns,
            key_types,
        ) in rows
    }
    if actual != expected:
        raise RuntimeError(
            "all canonical runtime tables require ordinary one-column UUID primary keys"
        )


def _require_retained_legacy_parents(connection: Connection) -> None:
    legacy_names = [
        f"{table_name}__partitioned_legacy_0003" for table_name in CANONICAL_TABLES
    ]
    rows = connection.exec_driver_sql(
        """SELECT relation.relname, relation.relkind::text
           FROM pg_class AS relation
           JOIN pg_namespace AS namespace
             ON namespace.oid = relation.relnamespace
           WHERE namespace.nspname = 'public'
             AND relation.relname = ANY($1::text[])""",
        (legacy_names,),
    )
    actual = {table_name: relation_kind for table_name, relation_kind in rows}
    if actual != {table_name: "p" for table_name in legacy_names}:
        raise RuntimeError("retained legacy partition parents are required")


def _require_no_partition_helpers(connection: Connection) -> None:
    rows = connection.exec_driver_sql(
        """SELECT table_name, column_name
           FROM information_schema.columns
           WHERE table_schema = 'public'
             AND table_name = ANY($1::text[])
             AND (column_name = 'partition_timestamp'
                  OR column_name LIKE '%\\_partition_timestamp' ESCAPE '\\')""",
        ([*CANONICAL_TABLES, *DEPENDENT_TABLES],),
    )
    if rows.first() is not None:
        raise RuntimeError("partition helper columns are forbidden")


def _require_evidence_fidelity(connection: Connection) -> None:
    columns = _column_catalog(connection, "evidence_records")
    required = {
        "structured_data": ("jsonb", "NO"),
        "raw_result": ("bytea", "NO"),
        "metadata": ("jsonb", "NO"),
        "content_hash": ("text", "NO"),
    }
    if (
        not required.items() <= columns.items()
        or "raw_result_reference" in columns
        or not _has_required_check(
            connection,
            "evidence_records",
            "ck_evidence_records_metadata_object",
            column_name="metadata",
            literals={"object"},
            fragments=("jsonb_typeof(metadata)",),
        )
    ):
        raise RuntimeError("evidence_records fidelity schema is required")

    report_columns = _column_catalog(connection, "rca_reports")
    if report_columns.get("result_status") != ("text", "NO") or not (
        _has_required_check(
            connection,
            "rca_reports",
            "ck_rca_reports_result_status",
            column_name="result_status",
            literals={"COMPLETE", "PARTIAL", "FAILED"},
        )
    ):
        raise RuntimeError("rca_reports.result_status Worker 0002 check is required")


def _require_worker_lifecycle_and_analysis(connection: Connection) -> None:
    for table_name in ("rca_runs", "specialist_runs", "worker_attempts"):
        columns = _column_catalog(connection, table_name)
        if columns.get("failure_code") != ("text", "YES") or not (
            _has_required_check(
                connection,
                table_name,
                f"ck_{table_name}_failure_code",
                column_name="failure_code",
                literals=WORKER_FAILURE_CODES,
                fragments=("failure_code is null",),
            )
        ):
            raise RuntimeError("Worker 0002 failure_code lifecycle schema is required")

    worker_job_columns = _column_catalog(connection, "worker_jobs")
    required_worker_job_columns = {
        "lease_owner": ("text", "YES"),
        "lease_expires_at": ("timestamptz", "YES"),
        "attempt_count": ("int4", "NO"),
    }
    if (
        not required_worker_job_columns.items() <= worker_job_columns.items()
        or not _has_required_check(
            connection,
            "worker_jobs",
            "ck_worker_jobs_attempt_count",
            column_name="attempt_count",
            literals=set(),
            fragments=("attempt_count >= 0", "attempt_count <= 3"),
        )
        or not _has_required_check(
            connection,
            "worker_jobs",
            "ck_worker_jobs_lease_pair",
            column_name="lease_owner",
            literals=set(),
            fragments=("lease_owner is null", "lease_expires_at is null"),
        )
        or not _has_index(
            connection,
            "ix_worker_jobs_claim",
            fragments=("worker_jobs", "(status, available_at, lease_expires_at)"),
        )
    ):
        raise RuntimeError("worker_jobs Worker 0002 lifecycle schema is required")

    specialist_columns = _column_catalog(connection, "specialist_runs")
    required_specialist_columns = {
        "analysis_result": ("jsonb", "YES"),
        "model_name": ("text", "YES"),
        "skill_name": ("text", "YES"),
        "skill_sha256": ("text", "YES"),
        "analyzed_at": ("timestamptz", "YES"),
    }
    if (
        not required_specialist_columns.items() <= specialist_columns.items()
        or not _has_required_check(
            connection,
            "specialist_runs",
            "ck_specialist_runs_analysis_result_object",
            column_name="analysis_result",
            literals={"object"},
            fragments=("jsonb_typeof(analysis_result)",),
        )
        or not _has_required_check(
            connection,
            "specialist_runs",
            "ck_specialist_runs_skill_sha256",
            column_name="skill_sha256",
            literals={"^[0-9a-f]{64}$"},
        )
        or not _has_required_check(
            connection,
            "specialist_runs",
            "ck_specialist_runs_status",
            column_name="status",
            literals=WORKER_SPECIALIST_STATUSES,
        )
    ):
        raise RuntimeError("specialist_runs Worker 0002 analysis schema is required")


def _require_uuid_only_dependent_foreign_keys(connection: Connection) -> None:
    rows = connection.exec_driver_sql(
        """SELECT relation.relname,
                  array_agg(source_column.attname ORDER BY key_row.ordinality),
                  target_relation.relname,
                  array_agg(target_column.attname ORDER BY key_row.ordinality),
                  array_agg(source_type.typname ORDER BY key_row.ordinality),
                  array_agg(target_type.typname ORDER BY key_row.ordinality)
           FROM pg_constraint AS constraint_row
           JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid
           JOIN pg_namespace AS source_namespace
             ON source_namespace.oid = relation.relnamespace
           JOIN pg_class AS target_relation
             ON target_relation.oid = constraint_row.confrelid
           JOIN pg_namespace AS target_namespace
             ON target_namespace.oid = target_relation.relnamespace
           JOIN unnest(constraint_row.conkey, constraint_row.confkey)
                WITH ORDINALITY AS key_row(source_num, target_num, ordinality)
             ON true
           JOIN pg_attribute AS source_column
             ON source_column.attrelid = relation.oid
            AND source_column.attnum = key_row.source_num
           JOIN pg_attribute AS target_column
             ON target_column.attrelid = target_relation.oid
            AND target_column.attnum = key_row.target_num
           JOIN pg_type AS source_type ON source_type.oid = source_column.atttypid
           JOIN pg_type AS target_type ON target_type.oid = target_column.atttypid
           WHERE constraint_row.contype = 'f'
             AND source_namespace.nspname = 'public'
             AND target_namespace.nspname = 'public'
             AND target_relation.relname = ANY($1::text[])
           GROUP BY relation.relname, target_relation.relname,
                    constraint_row.conname""",
        (list(CANONICAL_TABLES),),
    )
    actual = {
        (
            source_table,
            tuple(source_columns),
            target_table,
            tuple(target_columns),
            tuple(source_types),
            tuple(target_types),
        )
        for (
            source_table,
            source_columns,
            target_table,
            target_columns,
            source_types,
            target_types,
        ) in rows
    }
    expected = {
        (
            "ingestion_dedup_keys",
            ("delivery_id",),
            "webhook_deliveries",
            ("id",),
            ("uuid",),
            ("uuid",),
        ),
        (
            "alert_events",
            ("delivery_id",),
            "webhook_deliveries",
            ("id",),
            ("uuid",),
            ("uuid",),
        ),
        (
            "alert_instances",
            ("latest_event_id",),
            "alert_events",
            ("id",),
            ("uuid",),
            ("uuid",),
        ),
        (
            "incident_alerts",
            ("alert_event_id",),
            "alert_events",
            ("id",),
            ("uuid",),
            ("uuid",),
        ),
        (
            "hypothesis_evidence",
            ("evidence_id",),
            "evidence_records",
            ("id",),
            ("uuid",),
            ("uuid",),
        ),
    }
    if actual != expected:
        raise RuntimeError("dependent foreign keys must be UUID-only")


def upgrade() -> None:
    """Validate the post-conversion catalog."""
    connection = op.get_bind()
    _require_version(
        connection,
        "alembic_version_backend",
        BACKEND_REQUIRED_REVISION,
        "Backend",
    )
    _require_version(
        connection,
        "alembic_version_rca_worker",
        WORKER_REQUIRED_REVISION,
        "Worker",
    )
    _require_ordinary_uuid_primary_keys(connection)
    _require_retained_legacy_parents(connection)
    _require_no_partition_helpers(connection)
    _require_evidence_fidelity(connection)
    _require_worker_lifecycle_and_analysis(connection)
    _require_uuid_only_dependent_foreign_keys(connection)
    op.execute(
        "ALTER TABLE public.alembic_version_rca_worker "
        "ALTER COLUMN version_num TYPE VARCHAR(64)"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Worker 0003 is a forward validation gate; do not downgrade across the "
        "ordinary-table conversion"
    )
