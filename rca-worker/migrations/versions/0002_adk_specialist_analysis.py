"""Persist validated specialist analysis audit data."""

import re
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
SPECIALIST_ONLY_FAILURE_CODE_MAPPINGS = (
    ("NO_SAFE_MCP_CAPABILITY", "POLICY_DENIED"),
    ("MCP_PAYLOAD_TOO_LARGE", "VALIDATION_FAILED"),
    ("MCP_RESULT_INVALID", "VALIDATION_FAILED"),
    ("ANALYSIS_TIMEOUT", "DEADLINE_EXCEEDED"),
    ("ANALYSIS_SCHEMA_INVALID", "VALIDATION_FAILED"),
    ("ANALYSIS_UNKNOWN_EVIDENCE", "VALIDATION_FAILED"),
    ("ANALYSIS_INPUT_TRUNCATED", "VALIDATION_FAILED"),
    ("ANALYSIS_FAILED", "INTERNAL_ERROR"),
)
SQL_TEXT_LITERAL = re.compile(r"'((?:''|[^'])*)'(?:\s*::text)?")


def _check(values: tuple[str, ...], column: str) -> str:
    allowlist = ", ".join(f"'{value}'" for value in values)
    return f"{column} IS NULL OR {column} IN ({allowlist})"


def _required_check(values: tuple[str, ...], column: str) -> str:
    allowlist = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowlist})"


def _check_skeleton(sql: str) -> str:
    without_literals = SQL_TEXT_LITERAL.sub("?", sql)
    return re.sub(r"[\s()]", "", without_literals).upper()


def _check_literals(sql: str) -> set[str]:
    return {
        match.group(1).replace("''", "'") for match in SQL_TEXT_LITERAL.finditer(sql)
    }


def _matches_legacy_allowlist_check(
    candidate: sa.RowMapping,
    column_name: str,
    values: tuple[str, ...],
    *,
    nullable: bool,
) -> bool:
    if set(candidate["column_names"]) != {column_name}:
        return False

    definition = candidate["definition"]
    expression = candidate["expression"]
    if not isinstance(definition, str) or not isinstance(expression, str):
        return False
    if _check_literals(definition) != set(values):
        return False
    if _check_literals(expression) != set(values):
        return False

    placeholders = ",".join("?" for _ in values)
    allowlist_skeleton = f"{column_name}=ANYARRAY[{placeholders}]".upper()
    if nullable:
        allowlist_skeleton = f"{column_name}ISNULLOR{allowlist_skeleton}".upper()
    return (
        _check_skeleton(expression) == allowlist_skeleton
        and _check_skeleton(definition) == f"CHECK{allowlist_skeleton}"
    )


def _required_check_name(table_name: str, column_name: str) -> str:
    candidates = tuple(
        op.get_bind()
        .execute(
            sa.text(
                """SELECT constraint_row.conname AS constraint_name,
                          ARRAY(
                              SELECT referenced_column.attname
                              FROM unnest(constraint_row.conkey) WITH ORDINALITY
                                   AS key_row(attnum, ordinal_position)
                              JOIN pg_attribute AS referenced_column
                                ON referenced_column.attrelid =
                                   constraint_row.conrelid
                               AND referenced_column.attnum = key_row.attnum
                              ORDER BY key_row.ordinal_position
                          ) AS column_names,
                          pg_get_constraintdef(constraint_row.oid, true)
                              AS definition,
                          pg_get_expr(
                              constraint_row.conbin,
                              constraint_row.conrelid,
                              true
                          ) AS expression
                   FROM pg_constraint AS constraint_row
                   WHERE constraint_row.contype = 'c'
                     AND constraint_row.conrelid =
                         to_regclass(:qualified_table)
                     AND EXISTS (
                         SELECT 1
                         FROM unnest(constraint_row.conkey)
                              AS target_key(attnum)
                         JOIN pg_attribute AS target_column
                           ON target_column.attrelid = constraint_row.conrelid
                          AND target_column.attnum = target_key.attnum
                         WHERE target_column.attname = :column_name
                     )
                   ORDER BY constraint_row.conname"""
            ),
            {
                "qualified_table": f"public.{table_name}",
                "column_name": column_name,
            },
        )
        .mappings()
    )

    if table_name == "specialist_runs" and column_name == "status":
        expected_values = LEGACY_SPECIALIST_STATUSES
        nullable = False
    elif table_name in LIFECYCLE_TABLES and column_name == "failure_code":
        expected_values = LEGACY_FAILURE_CODES
        nullable = True
    else:
        raise RuntimeError(
            f"unsupported legacy allowlist CHECK: {table_name}.{column_name}"
        )

    matches = tuple(
        candidate
        for candidate in candidates
        if _matches_legacy_allowlist_check(
            candidate,
            column_name,
            expected_values,
            nullable=nullable,
        )
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one {table_name}.{column_name} legacy allowlist "
            f"CHECK; found {len(matches)}"
        )
    return matches[0]["constraint_name"]


def _replace_legacy_check(
    table_name: str,
    column_name: str,
    constraint_name: str,
    condition: str,
) -> None:
    existing_name = _required_check_name(table_name, column_name)
    op.drop_constraint(existing_name, table_name, type_="check")
    op.create_check_constraint(constraint_name, table_name, condition)


def _downgrade_failure_codes(connection: sa.Connection, table_name: str) -> None:
    if table_name not in LIFECYCLE_TABLES:
        raise ValueError(f"unsupported lifecycle table: {table_name}")
    cases = "\n".join(
        f"WHEN '{source}' THEN '{target}'"
        for source, target in SPECIALIST_ONLY_FAILURE_CODE_MAPPINGS
    )
    specialist_only_codes = ", ".join(
        f"'{source}'" for source, _ in SPECIALIST_ONLY_FAILURE_CODE_MAPPINGS
    )
    connection.execute(
        sa.text(
            f"""UPDATE {table_name}
                SET failure_code = CASE failure_code
                    {cases}
                    ELSE failure_code
                END
                WHERE failure_code IN ({specialist_only_codes})"""
        )
    )


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
    _replace_legacy_check(
        "specialist_runs",
        "status",
        "ck_specialist_runs_status",
        _required_check(SPECIALIST_STATUSES, "status"),
    )
    for table_name in LIFECYCLE_TABLES:
        _replace_legacy_check(
            table_name,
            "failure_code",
            f"ck_{table_name}_failure_code",
            _check(FAILURE_CODES, "failure_code"),
        )


def downgrade() -> None:
    op.execute("UPDATE specialist_runs SET status = 'FAILED' WHERE status = 'PARTIAL'")
    for table_name in LIFECYCLE_TABLES:
        _downgrade_failure_codes(op.get_bind(), table_name)
        op.drop_constraint(f"ck_{table_name}_failure_code", table_name, type_="check")
        op.create_check_constraint(
            f"ck_{table_name}_failure_code",
            table_name,
            _check(LEGACY_FAILURE_CODES, "failure_code"),
        )
    op.drop_constraint("ck_specialist_runs_status", "specialist_runs", type_="check")
    op.create_check_constraint(
        "ck_specialist_runs_status",
        "specialist_runs",
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
