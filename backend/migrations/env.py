from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from sre_agent.persistence.alembic_versions import reconcile_backend_version_tables
from sre_agent.persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _load_migration_environment() -> None:
    override_file = os.getenv("BACKEND_MIGRATION_ENV_FILE")
    environment_file = (
        Path(override_file) if override_file else Path.cwd() / ".env.backend-migration"
    )
    if override_file and not environment_file.is_file():
        raise RuntimeError("Backend migration environment file is unavailable")
    if environment_file.is_file():
        load_dotenv(environment_file, override=False)


_load_migration_environment()

database_url = os.getenv("MIGRATION_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="alembic_version_backend",
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    reconcile_backend_version_tables(connection)
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table="alembic_version_backend",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
