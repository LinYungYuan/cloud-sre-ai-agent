import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
)

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


@pytest.mark.asyncio
async def test_worker_schema_has_leases_attempts_and_all_safe_failure_codes() -> None:
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        columns = (
            (
                await connection.execute(
                    text(
                        """SELECT table_name, column_name
                           FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name IN (
                               'worker_jobs', 'rca_runs', 'specialist_runs',
                               'worker_attempts'
                             )"""
                    )
                )
            )
            .tuples()
            .all()
        )
        catalog = {}
        for table_name, column_name in columns:
            catalog.setdefault(table_name, set()).add(column_name)

        assert {"lease_owner", "lease_expires_at", "attempt_count"} <= catalog[
            "worker_jobs"
        ]
        for table_name in ("rca_runs", "specialist_runs", "worker_attempts"):
            assert "failure_code" in catalog[table_name]
            assert "error_message" not in catalog[table_name]

        constraints = "\n".join(
            (
                await connection.execute(
                    text(
                        """SELECT pg_get_constraintdef(oid)
                           FROM pg_constraint
                           WHERE connamespace = 'public'::regnamespace
                             AND conrelid IN (
                               'worker_jobs'::regclass,
                               'rca_runs'::regclass,
                               'specialist_runs'::regclass,
                               'worker_attempts'::regclass
                             )"""
                    )
                )
            ).scalars()
        )
        assert "attempt_count >= 0" in constraints
        assert "attempt_count <= 3" in constraints
        assert "failure_code" in constraints

        fixtures = {
            "test_rca_runs": (
                "rca_runs",
                "incident_id, status, failure_code",
                "gen_random_uuid(), 'FAILED', :code",
            ),
            "test_specialist_runs": (
                "specialist_runs",
                "rca_run_id, specialist_type, status, failure_code",
                "gen_random_uuid(), 'METRICS', 'FAILED', :code",
            ),
            "test_worker_attempts": (
                "worker_attempts",
                "worker_job_id, attempt_number, failure_code",
                "gen_random_uuid(), 1, :code",
            ),
        }
        for fixture_table, (source_table, columns, values) in fixtures.items():
            await connection.execute(
                text(
                    f"CREATE TEMP TABLE {fixture_table} "
                    f"(LIKE {source_table} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"
                )
            )
            for code in FAILURE_CODES:
                await connection.execute(
                    text(f"INSERT INTO {fixture_table} ({columns}) VALUES ({values})"),
                    {"code": code},
                )
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            f"INSERT INTO {fixture_table} ({columns}) VALUES ({values})"
                        ),
                        {"code": "UNKNOWN_FAILURE_CODE"},
                    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_specialist_analysis_audit_schema_enforces_safe_values() -> None:
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        columns = {
            row.column_name: (row.data_type, row.is_nullable)
            for row in (
                await connection.execute(
                    text(
                        """SELECT column_name, data_type, is_nullable
                           FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name = 'specialist_runs'"""
                    )
                )
            )
        }
        assert columns["analysis_result"] == ("jsonb", "YES")
        assert columns["model_name"] == ("text", "YES")
        assert columns["skill_name"] == ("text", "YES")
        assert columns["skill_sha256"] == ("text", "YES")
        assert columns["analyzed_at"] == ("timestamp with time zone", "YES")

        await connection.execute(
            text(
                """CREATE TEMP TABLE test_specialist_analysis
                   (LIKE specialist_runs INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"""
            )
        )
        for status in (
            "QUEUED",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "SKIPPED",
            "PARTIAL",
        ):
            await connection.execute(
                text(
                    """INSERT INTO test_specialist_analysis (
                           rca_run_id, specialist_type, status, analysis_result,
                           skill_sha256
                       ) VALUES (
                           gen_random_uuid(), 'METRICS', :status,
                           '{}'::jsonb, :skill_sha256
                       )"""
                ),
                {"status": status, "skill_sha256": "a" * 64},
            )

        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        """INSERT INTO test_specialist_analysis (
                               rca_run_id, specialist_type, status
                           ) VALUES (
                               gen_random_uuid(), 'METRICS', 'UNKNOWN'
                           )"""
                    )
                )

        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        """INSERT INTO test_specialist_analysis (
                               rca_run_id, specialist_type, status, analysis_result
                           ) VALUES (
                               gen_random_uuid(), 'LOGS', 'SUCCEEDED',
                               '"not-an-object"'::jsonb
                           )"""
                    )
                )

        for invalid_sha in ("A" * 64, "a" * 63, "g" * 64):
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """INSERT INTO test_specialist_analysis (
                                   rca_run_id, specialist_type, status, skill_sha256
                               ) VALUES (
                                   gen_random_uuid(), 'TRACES', 'SUCCEEDED',
                                   :skill_sha256
                               )"""
                        ),
                        {"skill_sha256": invalid_sha},
                    )

        version = await connection.scalar(
            text("SELECT version_num FROM alembic_version_rca_worker")
        )
        assert version == "0002_adk_specialist_analysis"
    await engine.dispose()


@pytest.mark.asyncio
async def test_evidence_and_reports_store_exact_safe_results() -> None:
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        evidence_columns = set(
            (
                await connection.execute(
                    text(
                        """SELECT column_name FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name = 'evidence_records'"""
                    )
                )
            ).scalars()
        )
        assert {"raw_result", "structured_data", "metadata", "content_hash"} <= (
            evidence_columns
        )
        assert "raw_result_reference" not in evidence_columns

        raw_type = await connection.scalar(
            text(
                """SELECT data_type FROM information_schema.columns
                   WHERE table_schema = 'public'
                     AND table_name = 'evidence_records'
                     AND column_name = 'raw_result'"""
            )
        )
        assert raw_type == "bytea"

        report_constraints = "\n".join(
            (
                await connection.execute(
                    text(
                        """SELECT pg_get_constraintdef(oid)
                           FROM pg_constraint
                           WHERE conrelid = 'rca_reports'::regclass"""
                    )
                )
            ).scalars()
        )
        assert "result_status" in report_constraints
        for status in ("COMPLETE", "PARTIAL", "FAILED"):
            assert status in report_constraints

    await engine.dispose()
