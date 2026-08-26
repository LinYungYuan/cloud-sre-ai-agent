import importlib.util
import os
import sys
import types
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from sre_agent.persistence.alembic_versions import (
    reconcile_backend_version_tables,
)

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
)


class _MigrationConfig:
    config_file_name = None
    config_ini_section = "alembic"

    def __init__(self) -> None:
        self.options = {
            "sqlalchemy.url": "postgresql+asyncpg://configured-default.invalid/backend"
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


def _load_backend_migration_env(monkeypatch: pytest.MonkeyPatch) -> _MigrationConfig:
    context = _OfflineAlembicContext()
    alembic = types.ModuleType("alembic")
    alembic.context = context
    monkeypatch.setitem(sys.modules, "alembic", alembic)
    module_path = Path(__file__).parents[3] / "migrations" / "env.py"
    spec = importlib.util.spec_from_file_location(
        f"backend_migration_env_{uuid4().hex}", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return context.config


def test_backend_migration_loads_only_its_default_environment_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend_url = "postgresql+asyncpg://backend-file.invalid/sre_agent"
    (tmp_path / ".env.backend-migration").write_text(f"DATABASE_URL={backend_url}\n")
    (tmp_path / ".env.rca-worker-migration").write_text(
        "DATABASE_URL=postgresql+asyncpg://worker-file.invalid/sre_agent\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MIGRATION_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("BACKEND_MIGRATION_ENV_FILE", raising=False)
    monkeypatch.delenv("RCA_WORKER_MIGRATION_ENV_FILE", raising=False)

    config = _load_backend_migration_env(monkeypatch)

    assert config.get_main_option("sqlalchemy.url") == backend_url


def test_backend_migration_loads_an_explicit_backend_environment_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend_url = "postgresql+asyncpg://backend-override.invalid/sre_agent"
    environment_file = tmp_path / "backend.env"
    environment_file.write_text(f"DATABASE_URL={backend_url}\n")
    monkeypatch.setenv("BACKEND_MIGRATION_ENV_FILE", str(environment_file))
    monkeypatch.setenv("RCA_WORKER_MIGRATION_ENV_FILE", str(tmp_path / "worker.env"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MIGRATION_TEST_DATABASE_URL", raising=False)

    config = _load_backend_migration_env(monkeypatch)

    assert config.get_main_option("sqlalchemy.url") == backend_url


def test_backend_migration_keeps_os_database_url_over_its_environment_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env.backend-migration").write_text(
        "DATABASE_URL=postgresql+asyncpg://backend-file.invalid/sre_agent\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://os.invalid/sre_agent")
    monkeypatch.delenv("MIGRATION_TEST_DATABASE_URL", raising=False)

    config = _load_backend_migration_env(monkeypatch)

    assert (
        config.get_main_option("sqlalchemy.url")
        == "postgresql+asyncpg://os.invalid/sre_agent"
    )


def test_backend_migration_preserves_database_url_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env.backend-migration").write_text(
        "DATABASE_URL=postgresql+asyncpg://backend-file.invalid/sre_agent\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://os.invalid/sre_agent")
    monkeypatch.setenv(
        "MIGRATION_TEST_DATABASE_URL", "postgresql+asyncpg://test.invalid/sre_agent"
    )

    config = _load_backend_migration_env(monkeypatch)

    assert (
        config.get_main_option("sqlalchemy.url")
        == "postgresql+asyncpg://test.invalid/sre_agent"
    )


def test_backend_migration_rejects_a_missing_explicit_environment_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing_file = tmp_path / "missing.env"
    connection_string = (
        "postgresql+asyncpg://username:password@database.invalid/sre_agent"
    )
    monkeypatch.setenv("BACKEND_MIGRATION_ENV_FILE", str(missing_file))
    monkeypatch.setenv("DATABASE_URL", connection_string)

    with pytest.raises(
        RuntimeError, match="Backend migration environment file"
    ) as error:
        _load_backend_migration_env(monkeypatch)

    assert connection_string not in str(error.value)


async def _table_names(connection, schema: str) -> set[str]:
    rows = await connection.execute(
        text(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = :schema"""
        ),
        {"schema": schema},
    )
    return set(rows.scalars())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("legacy", "backend", "expected"),
    [
        (None, None, set()),
        ("0002", None, {"alembic_version_backend"}),
        ("0002", "0002", {"alembic_version_backend"}),
    ],
)
async def test_reconciles_supported_backend_version_catalog_states(
    legacy: str | None,
    backend: str | None,
    expected: set[str],
) -> None:
    engine = create_async_engine(DATABASE_URL)
    schema = f"version_test_{uuid4().hex}"
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        if legacy is not None:
            await connection.exec_driver_sql(
                f'CREATE TABLE "{schema}".alembic_version '
                "(version_num TEXT PRIMARY KEY)"
            )
            await connection.exec_driver_sql(
                f'INSERT INTO "{schema}".alembic_version VALUES ($1)',
                (legacy,),
            )
        if backend is not None:
            await connection.exec_driver_sql(
                f'CREATE TABLE "{schema}".alembic_version_backend '
                "(version_num TEXT PRIMARY KEY)"
            )
            await connection.exec_driver_sql(
                f'INSERT INTO "{schema}".alembic_version_backend VALUES ($1)',
                (backend,),
            )

        await connection.run_sync(
            lambda sync_connection: reconcile_backend_version_tables(
                sync_connection, schema=schema
            )
        )

        assert await _table_names(connection, schema) == expected
    await engine.dispose()


@pytest.mark.asyncio
async def test_conflicting_backend_version_tables_fail_without_modification() -> None:
    engine = create_async_engine(DATABASE_URL)
    schema = f"version_test_{uuid4().hex}"
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        for table_name, revision in (
            ("alembic_version", "0001"),
            ("alembic_version_backend", "0002"),
        ):
            await connection.exec_driver_sql(
                f'CREATE TABLE "{schema}"."{table_name}" (version_num TEXT PRIMARY KEY)'
            )
            await connection.exec_driver_sql(
                f'INSERT INTO "{schema}"."{table_name}" VALUES ($1)',
                (revision,),
            )

        savepoint = await connection.begin_nested()
        with pytest.raises(DBAPIError, match="conflicting Alembic version tables"):
            await connection.run_sync(
                lambda sync_connection: reconcile_backend_version_tables(
                    sync_connection, schema=schema
                )
            )
        await savepoint.rollback()

        assert await _table_names(connection, schema) == {
            "alembic_version",
            "alembic_version_backend",
        }
        await transaction.rollback()
    await engine.dispose()
