import asyncio
import importlib.util
import os
import sys
import types
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent",
)
WORKER_ROOT = Path(__file__).resolve().parents[3]
REPOSITORY_ROOT = WORKER_ROOT.parent
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT / "src"))


def _asyncpg_url(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _with_database(database_url: str, database_name: str) -> str:
    parsed = urlsplit(database_url)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database_name}", "", ""))


def _upgrade(root: Path, database_url: str, revision: str) -> None:
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    with patch.dict(os.environ, {"MIGRATION_TEST_DATABASE_URL": database_url}):
        command.upgrade(config, revision)


@asynccontextmanager
async def _final_schema_database() -> AsyncIterator[str]:
    parsed = urlsplit(DATABASE_URL)
    if parsed.hostname != "127.0.0.1" or parsed.port != 5432:
        raise RuntimeError(
            "Worker schema tests require disposable PostgreSQL at 127.0.0.1:5432"
        )
    database_name = f"task2_worker_schema_{uuid4().hex}"
    admin = await asyncpg.connect(
        _asyncpg_url(_with_database(DATABASE_URL, "postgres"))
    )
    database_url = _with_database(DATABASE_URL, database_name)
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        created = True
        await asyncio.to_thread(
            _upgrade, BACKEND_ROOT, database_url, "0002_grafana_normalization_v2"
        )
        await asyncio.to_thread(
            _upgrade, WORKER_ROOT, database_url, "0002_adk_specialist_analysis"
        )
        await asyncio.to_thread(
            _upgrade, BACKEND_ROOT, database_url, "0003_non_partition_runtime_tables"
        )
        await asyncio.to_thread(
            _upgrade,
            WORKER_ROOT,
            database_url,
            "0003_validate_ordinary_runtime_tables",
        )
        yield database_url
    finally:
        if created:
            await admin.execute(
                f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
            )
        await admin.close()


@pytest_asyncio.fixture
async def final_schema_database_url() -> AsyncIterator[str]:
    async with _final_schema_database() as database_url:
        yield database_url


class _MigrationConfig:
    config_file_name = None
    config_ini_section = "alembic"

    def __init__(self) -> None:
        self.options = {
            "sqlalchemy.url": "postgresql+asyncpg://configured-default.invalid/rca_worker"
        }

    def get_main_option(self, name: str) -> str:
        return self.options[name]

    def set_main_option(self, name: str, value: str) -> None:
        self.options[name] = value

    def get_section(self, _name: str, _default: dict[str, str]) -> dict[str, str]:
        return self.options.copy()


class _OfflineAlembicContext:
    def __init__(self) -> None:
        self.config = _MigrationConfig()

    def configure(self, **_kwargs: object) -> None:
        pass

    def begin_transaction(self):
        return nullcontext()

    def is_offline_mode(self) -> bool:
        return True

    def run_migrations(self) -> None:
        pass


def _load_worker_migration_env(monkeypatch: pytest.MonkeyPatch) -> _MigrationConfig:
    context = _OfflineAlembicContext()
    alembic = types.ModuleType("alembic")
    alembic.__dict__["context"] = context
    monkeypatch.setitem(sys.modules, "alembic", alembic)
    module_path = Path(__file__).parents[3] / "migrations" / "env.py"
    spec = importlib.util.spec_from_file_location(
        f"worker_migration_env_{uuid4().hex}", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return context.config


def test_worker_migration_loads_only_its_default_environment_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worker_url = "postgresql+asyncpg://worker-file.invalid/sre_agent"
    (tmp_path / ".env.rca-worker-migration").write_text(f"DATABASE_URL={worker_url}\n")
    (tmp_path / ".env.backend-migration").write_text(
        "DATABASE_URL=postgresql+asyncpg://backend-file.invalid/sre_agent\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MIGRATION_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("BACKEND_MIGRATION_ENV_FILE", raising=False)
    monkeypatch.delenv("RCA_WORKER_MIGRATION_ENV_FILE", raising=False)

    config = _load_worker_migration_env(monkeypatch)

    assert config.get_main_option("sqlalchemy.url") == worker_url


def test_worker_migration_loads_an_explicit_worker_environment_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worker_url = "postgresql+asyncpg://worker-override.invalid/sre_agent"
    environment_file = tmp_path / "worker.env"
    environment_file.write_text(f"DATABASE_URL={worker_url}\n")
    monkeypatch.setenv("RCA_WORKER_MIGRATION_ENV_FILE", str(environment_file))
    monkeypatch.setenv("BACKEND_MIGRATION_ENV_FILE", str(tmp_path / "backend.env"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MIGRATION_TEST_DATABASE_URL", raising=False)

    config = _load_worker_migration_env(monkeypatch)

    assert config.get_main_option("sqlalchemy.url") == worker_url


def test_worker_migration_keeps_os_database_url_over_its_environment_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env.rca-worker-migration").write_text(
        "DATABASE_URL=postgresql+asyncpg://worker-file.invalid/sre_agent\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://os.invalid/sre_agent")
    monkeypatch.delenv("MIGRATION_TEST_DATABASE_URL", raising=False)

    config = _load_worker_migration_env(monkeypatch)

    assert (
        config.get_main_option("sqlalchemy.url")
        == "postgresql+asyncpg://os.invalid/sre_agent"
    )


def test_worker_migration_preserves_database_url_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env.rca-worker-migration").write_text(
        "DATABASE_URL=postgresql+asyncpg://worker-file.invalid/sre_agent\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://os.invalid/sre_agent")
    monkeypatch.setenv(
        "MIGRATION_TEST_DATABASE_URL", "postgresql+asyncpg://test.invalid/sre_agent"
    )

    config = _load_worker_migration_env(monkeypatch)

    assert (
        config.get_main_option("sqlalchemy.url")
        == "postgresql+asyncpg://test.invalid/sre_agent"
    )


def test_worker_migration_rejects_a_missing_explicit_environment_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing_file = tmp_path / "missing.env"
    connection_string = (
        "postgresql+asyncpg://username:password@database.invalid/sre_agent"
    )
    monkeypatch.setenv("RCA_WORKER_MIGRATION_ENV_FILE", str(missing_file))
    monkeypatch.setenv("DATABASE_URL", connection_string)

    with pytest.raises(
        RuntimeError, match="Worker migration environment file"
    ) as error:
        _load_worker_migration_env(monkeypatch)

    assert connection_string not in str(error.value)


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
async def test_worker_schema_has_leases_attempts_and_all_safe_failure_codes(
    final_schema_database_url: str,
) -> None:
    engine = create_async_engine(final_schema_database_url)
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
async def test_specialist_analysis_audit_schema_enforces_safe_values(
    final_schema_database_url: str,
) -> None:
    engine = create_async_engine(final_schema_database_url)
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

        await connection.execute(
            text(
                """INSERT INTO test_specialist_analysis (
                       rca_run_id, specialist_type, status,
                       analysis_result, skill_sha256
                   ) VALUES (
                       gen_random_uuid(), 'LOGS', 'FAILED', NULL, NULL
                   )"""
            )
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
        assert version == "0003_validate_ordinary_runtime_tables"
    await engine.dispose()


@pytest.mark.asyncio
async def test_evidence_and_reports_store_exact_safe_results(
    final_schema_database_url: str,
) -> None:
    engine = create_async_engine(final_schema_database_url)
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
