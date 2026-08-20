import os
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
