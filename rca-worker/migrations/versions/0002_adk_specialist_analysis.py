"""Persist validated specialist analysis audit data."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002_adk_specialist_analysis"
down_revision: str | None = "0001_rca_worker_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_FAILURE_CODES = (
    "DEADLINE_EXCEEDED",
    "MCP_TIMEOUT",
    "MCP_TRANSPORT",
    "POLICY_DENIED",
    "VALIDATION_FAILED",
    "INTERNAL_ERROR",
)

SPECIALIST_FAILURE_CODES = (
    "NO_SAFE_MCP_CAPABILITY",
    "MCP_TIMEOUT",
    "MCP_TRANSPORT",
    "MCP_PAYLOAD_TOO_LARGE",
    "MCP_RESULT_INVALID",
    "ANALYSIS_TIMEOUT",
    "ANALYSIS_SCHEMA_INVALID",
    "ANALYSIS_UNKNOWN_EVIDENCE",
    "ANALYSIS_INPUT_TRUNCATED",
    "ANALYSIS_FAILED",
)

FAILURE_CODES = tuple(dict.fromkeys((*LEGACY_FAILURE_CODES, *SPECIALIST_FAILURE_CODES)))
SPECIALIST_STATUSES = (
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "SKIPPED",
    "PARTIAL",
)
LEGACY_SPECIALIST_STATUSES = SPECIALIST_STATUSES[:-1]
LIFECYCLE_TABLES = ("rca_runs", "specialist_runs", "worker_attempts")


def _check(values: tuple[str, ...], column: str) -> str:
    allowlist = ", ".join(f"'{value}'" for value in values)
    return f"{column} IS NULL OR {column} IN ({allowlist})"


def _required_check(values: tuple[str, ...], column: str) -> str:
    allowlist = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowlist})"


def _required_check_name(table_name: str, column_name: str) -> str:
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                """SELECT DISTINCT constraint_row.conname
               FROM pg_constraint AS constraint_row
               JOIN pg_attribute AS column_row
                 ON column_row.attrelid = constraint_row.conrelid
                AND column_row.attnum = ANY (constraint_row.conkey)
               WHERE constraint_row.contype = 'c'
                 AND constraint_row.conrelid = to_regclass(:qualified_table)
                 AND column_row.attname = :column_name
               ORDER BY constraint_row.conname"""
            ),
            {
                "qualified_table": f"public.{table_name}",
                "column_name": column_name,
            },
        )
        .scalars()
    )
    names = tuple(rows)
    if len(names) != 1:
        raise RuntimeError(
            f"expected exactly one {table_name}.{column_name} CHECK constraint; "
            f"found {len(names)}"
        )
    return names[0]


def _replace_check(
    table_name: str,
    column_name: str,
    constraint_name: str,
    condition: str,
) -> None:
    existing_name = _required_check_name(table_name, column_name)
    op.drop_constraint(existing_name, table_name, type_="check")
    op.create_check_constraint(constraint_name, table_name, condition)


def upgrade() -> None:
    op.add_column(
        "specialist_runs", sa.Column("analysis_result", JSONB(), nullable=True)
    )
    op.add_column("specialist_runs", sa.Column("model_name", sa.Text(), nullable=True))
    op.add_column("specialist_runs", sa.Column("skill_name", sa.Text(), nullable=True))
    op.add_column(
        "specialist_runs", sa.Column("skill_sha256", sa.Text(), nullable=True)
    )
    op.add_column(
        "specialist_runs",
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_specialist_runs_analysis_result_object",
        "specialist_runs",
        "analysis_result IS NULL OR jsonb_typeof(analysis_result) = 'object'",
    )
    op.create_check_constraint(
        "ck_specialist_runs_skill_sha256",
        "specialist_runs",
        "skill_sha256 IS NULL OR skill_sha256 ~ '^[0-9a-f]{64}$'",
    )
    _replace_check(
        "specialist_runs",
        "status",
        "ck_specialist_runs_status",
        _required_check(SPECIALIST_STATUSES, "status"),
    )
    for table_name in LIFECYCLE_TABLES:
        _replace_check(
            table_name,
            "failure_code",
            f"ck_{table_name}_failure_code",
            _check(FAILURE_CODES, "failure_code"),
        )


def downgrade() -> None:
    op.execute("UPDATE specialist_runs SET status = 'FAILED' WHERE status = 'PARTIAL'")
    for table_name in LIFECYCLE_TABLES:
        op.execute(
            f"""UPDATE {table_name}
                SET failure_code = CASE failure_code
                    WHEN 'NO_SAFE_MCP_CAPABILITY' THEN 'POLICY_DENIED'
                    WHEN 'MCP_PAYLOAD_TOO_LARGE' THEN 'VALIDATION_FAILED'
                    WHEN 'MCP_RESULT_INVALID' THEN 'VALIDATION_FAILED'
                    WHEN 'ANALYSIS_TIMEOUT' THEN 'DEADLINE_EXCEEDED'
                    WHEN 'ANALYSIS_SCHEMA_INVALID' THEN 'VALIDATION_FAILED'
                    WHEN 'ANALYSIS_UNKNOWN_EVIDENCE' THEN 'VALIDATION_FAILED'
                    WHEN 'ANALYSIS_INPUT_TRUNCATED' THEN 'VALIDATION_FAILED'
                    WHEN 'ANALYSIS_FAILED' THEN 'INTERNAL_ERROR'
                    ELSE failure_code
                END"""
        )
        _replace_check(
            table_name,
            "failure_code",
            f"ck_{table_name}_failure_code",
            _check(LEGACY_FAILURE_CODES, "failure_code"),
        )
    _replace_check(
        "specialist_runs",
        "status",
        "ck_specialist_runs_status",
        _required_check(LEGACY_SPECIALIST_STATUSES, "status"),
    )
    op.drop_constraint(
        "ck_specialist_runs_skill_sha256", "specialist_runs", type_="check"
    )
    op.drop_constraint(
        "ck_specialist_runs_analysis_result_object",
        "specialist_runs",
        type_="check",
    )
    op.drop_column("specialist_runs", "analyzed_at")
    op.drop_column("specialist_runs", "skill_sha256")
    op.drop_column("specialist_runs", "skill_name")
    op.drop_column("specialist_runs", "model_name")
    op.drop_column("specialist_runs", "analysis_result")
