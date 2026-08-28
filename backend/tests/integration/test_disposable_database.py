import asyncio
import sys
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from . import _disposable_database


class _CompletedMigrationProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.returncode: int | None = returncode
        self.stdout = stdout
        self.stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr


class _TimedOutMigrationProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.reaped = False
        self._terminated = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self._terminated.set()

    def kill(self) -> None:
        self.killed = True
        self._terminated.set()

    async def wait(self) -> int:
        await self._terminated.wait()
        self.returncode = -15
        self.reaped = True
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _RecordingAdmin:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.closed = False

    async def execute(self, statement: str) -> None:
        self.statements.append(statement)

    async def close(self) -> None:
        self.closed = True


class _NeverReapedProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.wait_calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _DropFailingAdmin:
    def __init__(self) -> None:
        self.closed = False

    async def execute(self, statement: str) -> None:
        if statement.startswith("DROP DATABASE"):
            raise asyncpg.PostgresError("drop unavailable")

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_migrations_run_the_exact_four_gates_with_the_disposable_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def create_subprocess_exec(
        *command: str, **options: Any
    ) -> _CompletedMigrationProcess:
        calls.append((command, options))
        return _CompletedMigrationProcess()

    monkeypatch.setattr(
        _disposable_database.asyncio,
        "create_subprocess_exec",
        create_subprocess_exec,
    )
    backend_root = Path("/repository/backend")
    database_url = (
        "postgresql+asyncpg://postgres@127.0.0.1:5432/fixture_unique_database"
    )

    await _disposable_database.run_migration_subprocess(
        database_url,
        backend_root=backend_root,
    )

    expected_gates = (
        ("backend", backend_root, "0002_grafana_normalization_v2"),
        (
            "worker",
            backend_root.parent / "rca-worker",
            "0002_adk_specialist_analysis",
        ),
        ("backend", backend_root, "0003_non_partition_runtime_tables"),
        (
            "worker",
            backend_root.parent / "rca-worker",
            "0003_validate_ordinary_runtime_tables",
        ),
    )
    assert len(calls) == len(expected_gates)
    for (command, options), (_, root, revision) in zip(
        calls, expected_gates, strict=True
    ):
        assert command == (
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(root / "alembic.ini"),
            "upgrade",
            revision,
        )
        assert options["cwd"] == root
        assert options["env"]["MIGRATION_TEST_DATABASE_URL"] == database_url
        assert revision not in {"head", "stamp"}


@pytest.mark.asyncio
async def test_failed_migration_names_the_gate_and_stops_later_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    processes = iter(
        (
            _CompletedMigrationProcess(),
            _CompletedMigrationProcess(
                returncode=1,
                stderr=b"worker gate failed",
            ),
        )
    )

    async def create_subprocess_exec(
        *command: str, **_options: Any
    ) -> _CompletedMigrationProcess:
        commands.append(command)
        return next(processes)

    monkeypatch.setattr(
        _disposable_database.asyncio,
        "create_subprocess_exec",
        create_subprocess_exec,
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "phase=migration-gate stream=worker "
            "revision=0002_adk_specialist_analysis"
        ),
    ):
        await _disposable_database.run_migration_subprocess(
            "postgresql+asyncpg://postgres@127.0.0.1:5432/fixture_failure",
            backend_root=Path("/repository/backend"),
        )

    assert len(commands) == 2


@pytest.mark.asyncio
async def test_timed_out_migration_names_the_gate_and_is_terminated_and_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _TimedOutMigrationProcess()

    async def create_subprocess_exec(
        *_command: str, **_options: Any
    ) -> _TimedOutMigrationProcess:
        return process

    monkeypatch.setattr(
        _disposable_database.asyncio,
        "create_subprocess_exec",
        create_subprocess_exec,
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "phase=migration-gate stream=backend "
            "revision=0002_grafana_normalization_v2 timeout"
        ),
    ):
        await _disposable_database.run_migration_subprocess(
            "postgresql+asyncpg://postgres@127.0.0.1:5432/fixture_timeout",
            backend_root=Path("/repository/backend"),
            timeout=0.01,
        )

    assert process.terminated
    assert not process.killed
    assert process.reaped


@pytest.mark.asyncio
async def test_disposable_database_drops_and_closes_after_migration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _RecordingAdmin()

    async def connect_admin_database(*_args: object, **_kwargs: object) -> _RecordingAdmin:
        return admin

    async def run_migration_subprocess(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("gate failed")

    monkeypatch.setattr(
        _disposable_database, "connect_admin_database", connect_admin_database
    )
    monkeypatch.setattr(
        _disposable_database, "run_migration_subprocess", run_migration_subprocess
    )

    with pytest.raises(RuntimeError, match="gate failed"):
        async with _disposable_database.disposable_database_url(
            "postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent",
            prefix="fixture_failure_cleanup",
            backend_root=Path.cwd(),
        ):
            pass

    assert any(
        statement.startswith("DROP DATABASE IF EXISTS")
        for statement in admin.statements
    )
    assert admin.closed


@pytest.mark.asyncio
async def test_terminate_and_reap_bounds_the_post_kill_wait() -> None:
    process = _NeverReapedProcess()

    with pytest.raises(RuntimeError, match="phase=process-reap-timeout"):
        await _disposable_database.terminate_and_reap_migration(
            process, timeout=0.01
        )

    assert process.terminated
    assert process.killed
    assert process.wait_calls == 2


@pytest.mark.asyncio
async def test_disposable_database_closes_admin_when_drop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _DropFailingAdmin()

    async def connect_admin_database(*_args, **_kwargs) -> _DropFailingAdmin:
        return admin

    async def run_migration_subprocess(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        _disposable_database, "connect_admin_database", connect_admin_database
    )
    monkeypatch.setattr(
        _disposable_database, "run_migration_subprocess", run_migration_subprocess
    )

    with pytest.raises(RuntimeError, match="phase=drop-database"):
        async with _disposable_database.disposable_database_url(
            "postgresql+asyncpg://postgres@localhost/sre_agent",
            prefix="task8_test",
            backend_root=Path.cwd(),
        ):
            pass

    assert admin.closed
