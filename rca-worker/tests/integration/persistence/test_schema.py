import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
)


@pytest.mark.asyncio
async def test_worker_schema_has_leases_attempts_and_safe_failure_codes() -> None:
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

        version = await connection.scalar(
            text("SELECT version_num FROM alembic_version_rca_worker")
        )
        assert version == "0001_rca_worker_v1"
    await engine.dispose()
