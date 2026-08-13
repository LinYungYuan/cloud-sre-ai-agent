"""Adopt legacy RCA tables and add durable Worker lifecycle fields."""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_rca_worker_v1"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FAILURE_CODES = (
    "DEADLINE_EXCEEDED",
    "MCP_TIMEOUT",
    "MCP_TRANSPORT",
    "POLICY_DENIED",
    "VALIDATION_FAILED",
    "INTERNAL_ERROR",
)

BASELINE_COLUMNS = {
    "rca_runs": {
        "id",
        "incident_id",
        "status",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
        "error_message",
    },
    "specialist_runs": {
        "id",
        "rca_run_id",
        "specialist_type",
        "status",
        "started_at",
        "completed_at",
        "created_at",
        "error_message",
    },
    "evidence_records": {
        "id",
        "partition_timestamp",
        "observed_at",
        "rca_run_id",
        "specialist_run_id",
        "evidence_type",
        "source_agent",
        "source_endpoint",
        "tool_name",
        "team_id",
        "project_id",
        "environment_id",
        "service_id",
        "time_window_start",
        "time_window_end",
        "structured_data",
        "raw_result_reference",
        "content_hash",
    },
    "rca_hypotheses": {
        "id",
        "rca_run_id",
        "statement",
        "confidence",
        "created_at",
    },
    "hypothesis_evidence": {
        "id",
        "hypothesis_id",
        "evidence_id",
        "evidence_partition_timestamp",
        "relation",
        "created_at",
    },
    "rca_reports": {
        "id",
        "rca_run_id",
        "version",
        "summary",
        "report",
        "created_at",
    },
    "worker_jobs": {
        "id",
        "rca_run_id",
        "job_type",
        "status",
        "payload",
        "available_at",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    },
    "worker_attempts": {
        "id",
        "worker_job_id",
        "attempt_number",
        "started_at",
        "completed_at",
        "error_message",
    },
}


def _require_backend_head() -> None:
    connection = op.get_bind()
    exists = connection.exec_driver_sql(
        "SELECT to_regclass('public.alembic_version_backend') IS NOT NULL"
    ).scalar_one()
    if not exists:
        raise RuntimeError(
            "Backend migration 0002_grafana_normalization_v2 is required"
        )
    backend_revision = connection.exec_driver_sql(
        """SELECT version_num FROM public.alembic_version_backend"""
    ).scalar_one_or_none()
    if backend_revision != "0002_grafana_normalization_v2":
        raise RuntimeError(
            "Backend migration 0002_grafana_normalization_v2 is required"
        )


def _require_exact_legacy_baseline() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        """SELECT table_name, column_name
           FROM information_schema.columns
           WHERE table_schema = 'public'
             AND table_name = ANY($1::text[])""",
        (list(BASELINE_COLUMNS),),
    )
    actual: dict[str, set[str]] = {table_name: set() for table_name in BASELINE_COLUMNS}
    for table_name, column_name in rows:
        actual[table_name].add(column_name)
    if actual != BASELINE_COLUMNS:
        raise RuntimeError("legacy RCA schema does not match the expected baseline")

    required_indexes = connection.exec_driver_sql(
        """SELECT to_regclass('public.uq_rca_runs_active_incident') IS NOT NULL,
                  to_regclass('public.uq_worker_jobs_run_type') IS NOT NULL"""
    ).one()
    if not all(required_indexes):
        raise RuntimeError("legacy RCA constraints do not match the expected baseline")


def _failure_check(column: str = "failure_code") -> str:
    values = ", ".join(f"'{value}'" for value in FAILURE_CODES)
    return f"{column} IS NULL OR {column} IN ({values})"


def upgrade() -> None:
    _require_backend_head()
    _require_exact_legacy_baseline()
    op.execute(
        """
        ALTER TABLE worker_jobs
            ADD COLUMN lease_owner TEXT NULL,
            ADD COLUMN lease_expires_at TIMESTAMPTZ NULL,
            ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0,
            ADD CONSTRAINT ck_worker_jobs_attempt_count
                CHECK (attempt_count >= 0 AND attempt_count <= 3),
            ADD CONSTRAINT ck_worker_jobs_lease_pair
                CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
        """
    )
    for table_name in ("rca_runs", "specialist_runs", "worker_attempts"):
        op.execute(
            f"""
            ALTER TABLE {table_name}
                ADD COLUMN failure_code TEXT NULL,
                ADD CONSTRAINT ck_{table_name}_failure_code
                    CHECK ({_failure_check()}),
                DROP COLUMN error_message
            """
        )
    op.execute(
        """
        ALTER TABLE evidence_records
            ADD COLUMN raw_result BYTEA NULL,
            ADD COLUMN metadata JSONB NULL
        """
    )
    op.execute(
        """
        UPDATE evidence_records
        SET raw_result = ''::bytea,
            metadata = jsonb_build_object(
                'legacyBackfill', true,
                'originalReferenceDiscarded', true
            )
        WHERE raw_result IS NULL OR metadata IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE evidence_records
            ALTER COLUMN raw_result SET NOT NULL,
            ALTER COLUMN metadata SET NOT NULL,
            ADD CONSTRAINT ck_evidence_records_metadata_object
                CHECK (jsonb_typeof(metadata) = 'object'),
            DROP COLUMN raw_result_reference
        """
    )
    op.execute(
        """
        ALTER TABLE rca_reports ADD COLUMN result_status TEXT NULL
        """
    )
    op.execute(
        """
        UPDATE rca_reports
        SET result_status = CASE report ->> 'status'
            WHEN 'FAILED' THEN 'FAILED'
            WHEN 'PARTIAL' THEN 'PARTIAL'
            ELSE 'COMPLETE'
        END
        WHERE result_status IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE rca_reports
            ALTER COLUMN result_status SET NOT NULL,
            ADD CONSTRAINT ck_rca_reports_result_status
                CHECK (result_status IN ('COMPLETE', 'PARTIAL', 'FAILED'))
        """
    )
    op.execute(
        """CREATE INDEX ix_worker_jobs_claim
           ON worker_jobs (status, available_at, lease_expires_at)"""
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_worker_jobs_claim")
    op.execute(
        """
        ALTER TABLE rca_reports
            DROP CONSTRAINT ck_rca_reports_result_status,
            DROP COLUMN result_status
        """
    )
    op.execute(
        """
        ALTER TABLE evidence_records
            ADD COLUMN raw_result_reference TEXT NULL
        """
    )
    op.execute(
        """
        UPDATE evidence_records
        SET raw_result_reference = 'worker-downgrade:exact-bytes-cannot-be-restored'
        WHERE raw_result_reference IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE evidence_records
            ALTER COLUMN raw_result_reference SET NOT NULL,
            DROP CONSTRAINT ck_evidence_records_metadata_object,
            DROP COLUMN metadata,
            DROP COLUMN raw_result
        """
    )
    for table_name in ("worker_attempts", "specialist_runs", "rca_runs"):
        op.execute(
            f"""
            ALTER TABLE {table_name}
                ADD COLUMN error_message TEXT NULL,
                DROP CONSTRAINT ck_{table_name}_failure_code,
                DROP COLUMN failure_code
            """
        )
    op.execute(
        """
        ALTER TABLE worker_jobs
            DROP CONSTRAINT ck_worker_jobs_lease_pair,
            DROP CONSTRAINT ck_worker_jobs_attempt_count,
            DROP COLUMN attempt_count,
            DROP COLUMN lease_expires_at,
            DROP COLUMN lease_owner
        """
    )
