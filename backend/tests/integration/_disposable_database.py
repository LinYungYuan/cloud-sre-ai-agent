import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg

LOGGER = logging.getLogger(__name__)
CONNECT_TIMEOUT_SECONDS = 10
ADMIN_STATEMENT_TIMEOUT_SECONDS = 10.0
ADMIN_CLOSE_TIMEOUT_SECONDS = 10.0
MIGRATION_LOCK_TIMEOUT_MILLISECONDS = 5_000
MIGRATION_STATEMENT_TIMEOUT_MILLISECONDS = 45_000
MIGRATION_OVERALL_TIMEOUT_SECONDS = 60.0
PROCESS_TERMINATION_TIMEOUT_SECONDS = 5.0
MIGRATION_GATES = (
    ("backend", "0002_grafana_normalization_v2"),
    ("worker", "0002_adk_specialist_analysis"),
    ("backend", "0003_non_partition_runtime_tables"),
    ("worker", "0003_validate_ordinary_runtime_tables"),
)


class MigrationProcess(Protocol):
    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


def _with_database(database_url: str, database_name: str) -> str:
    parsed = urlsplit(database_url)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database_name}", "", ""))


def _asyncpg_url(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _phase_error(phase: str, error: Exception) -> RuntimeError:
    return RuntimeError(
        "Task 8 disposable database setup failed "
        f"at phase={phase}: {error}. "
        "Verify PostgreSQL is reachable and the test role can create databases."
    )


async def connect_admin_database(database_url: str) -> asyncpg.Connection:
    phase = "admin-connect"
    LOGGER.info("Task 8 disposable database phase=%s", phase)
    try:
        return await asyncio.wait_for(
            asyncpg.connect(
                _asyncpg_url(_with_database(database_url, "postgres")),
                timeout=CONNECT_TIMEOUT_SECONDS,
            ),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError, asyncpg.PostgresError) as error:
        raise _phase_error(phase, error) from error


async def execute_admin_statement(
    admin: asyncpg.Connection,
    statement: str,
    *,
    phase: str,
) -> None:
    LOGGER.info("Task 8 disposable database phase=%s", phase)
    try:
        await asyncio.wait_for(
            admin.execute(statement), timeout=ADMIN_STATEMENT_TIMEOUT_SECONDS
        )
    except (TimeoutError, asyncpg.PostgresError) as error:
        raise _phase_error(phase, error) from error


async def close_admin_database(admin: asyncpg.Connection) -> None:
    phase = "admin-close"
    LOGGER.info("Task 8 disposable database phase=%s", phase)
    try:
        await asyncio.wait_for(admin.close(), timeout=ADMIN_CLOSE_TIMEOUT_SECONDS)
    except (TimeoutError, OSError, asyncpg.PostgresError) as error:
        raise _phase_error(phase, error) from error


async def terminate_and_reap_migration(
    process: MigrationProcess,
    *,
    timeout: float = PROCESS_TERMINATION_TIMEOUT_SECONDS,
) -> None:
    if process.returncode is not None:
        return

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        return
    except TimeoutError:
        process.kill()

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError as error:
        raise RuntimeError(
            "Task 8 disposable database setup failed "
            f"at phase=process-reap-timeout after kill wait of {timeout:.1f}s."
        ) from error


async def run_migration_subprocess(
    database_url: str,
    *,
    backend_root: Path,
    timeout: float = MIGRATION_OVERALL_TIMEOUT_SECONDS,
) -> None:
    environment = os.environ.copy()
    environment["MIGRATION_TEST_DATABASE_URL"] = database_url
    deadline = asyncio.get_running_loop().time() + timeout
    worker_root = backend_root.parent / "rca-worker"

    for stream, revision in MIGRATION_GATES:
        root = backend_root if stream == "backend" else worker_root
        phase = f"migration-gate stream={stream} revision={revision}"
        LOGGER.info("Task 8 disposable database phase=%s", phase)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(root / "alembic.ini"),
                "upgrade",
                revision,
                cwd=root,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise _phase_error(f"{phase} start", error) from error

        remaining_timeout = max(0.0, deadline - asyncio.get_running_loop().time())
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=remaining_timeout
            )
        except TimeoutError as error:
            await terminate_and_reap_migration(process)
            raise RuntimeError(
                "Task 8 disposable database setup failed "
                f"at phase={phase} timeout after {timeout:.1f}s overall; "
                "subprocess was terminated and reaped."
            ) from error

        if process.returncode != 0:
            output = (stdout + stderr).decode(errors="replace")[-2_000:]
            raise RuntimeError(
                "Task 8 disposable database setup failed "
                f"at phase={phase} with exit code {process.returncode}: {output}"
            )


@asynccontextmanager
async def disposable_database_url(
    database_url: str,
    *,
    prefix: str,
    backend_root: Path,
) -> AsyncIterator[str]:
    """Provision and force-drop a bounded disposable post-0003 database."""
    database_name = f"{prefix}_{uuid4().hex}"
    admin = await connect_admin_database(database_url)
    disposable_url = _with_database(database_url, database_name)
    database_created = False
    try:
        await execute_admin_statement(
            admin,
            f'CREATE DATABASE "{database_name}"',
            phase="create-database",
        )
        database_created = True
        await execute_admin_statement(
            admin,
            f"ALTER DATABASE \"{database_name}\" SET lock_timeout TO "
            f"'{MIGRATION_LOCK_TIMEOUT_MILLISECONDS}ms'",
            phase="configure-lock-timeout",
        )
        await execute_admin_statement(
            admin,
            f"ALTER DATABASE \"{database_name}\" SET statement_timeout TO "
            f"'{MIGRATION_STATEMENT_TIMEOUT_MILLISECONDS}ms'",
            phase="configure-statement-timeout",
        )
        await run_migration_subprocess(disposable_url, backend_root=backend_root)
        yield disposable_url
    finally:
        try:
            if database_created:
                await execute_admin_statement(
                    admin,
                    f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)',
                    phase="drop-database",
                )
        finally:
            await close_admin_database(admin)
