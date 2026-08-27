import asyncio
from pathlib import Path

import asyncpg
import pytest

from . import _disposable_database


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
