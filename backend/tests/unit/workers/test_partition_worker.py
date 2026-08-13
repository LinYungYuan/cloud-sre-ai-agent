from datetime import UTC, date, datetime

import pytest
from pydantic import SecretStr

from sre_agent.workers.partition_worker import (
    PartitionWorkerSettings,
    main,
    maintain_partition_runway,
    partition_runway,
)


def test_partition_runway_includes_current_and_next_two_across_year_boundary():
    assert partition_runway(date(2039, 12, 17)) == (
        date(2039, 12, 1),
        date(2040, 1, 1),
        date(2040, 2, 1),
    )


def test_partition_settings_redact_database_url():
    database_url = "postgresql+asyncpg://operator:do-not-leak@db/sre"

    settings = PartitionWorkerSettings(database_url=SecretStr(database_url))

    assert database_url not in repr(settings)
    assert "**********" in repr(settings)


class FailingConnection:
    def __init__(self) -> None:
        self.closed = False

    async def fetchval(self, query: str, *args: object) -> str:
        del query, args
        raise RuntimeError("partition drift")

    async def fetchrow(self, query: str, *args: object):
        del query, args

    async def execute(self, query: str, *args: object) -> str:
        del query, args
        return "CREATE TABLE"

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_partition_worker_closes_connection_when_maintenance_fails():
    connection = FailingConnection()
    observed_urls: list[str] = []

    async def connect(database_url: str) -> FailingConnection:
        observed_urls.append(database_url)
        return connection

    settings = PartitionWorkerSettings(
        database_url=SecretStr("postgresql+asyncpg://operator:do-not-leak@db/sre")
    )

    with pytest.raises(RuntimeError, match="partition drift"):
        await maintain_partition_runway(
            settings,
            connection_factory=connect,
            clock=lambda: datetime(2039, 12, 17, tzinfo=UTC),
        )

    assert observed_urls == ["postgresql://operator:do-not-leak@db/sre"]
    assert connection.closed


def test_partition_worker_main_returns_nonzero_without_leaking_failure(capsys):
    secret = "operator:do-not-leak"

    async def fail() -> None:
        raise RuntimeError(f"database failed for {secret}")

    assert main(run=fail) == 1
    captured = capsys.readouterr()
    assert "partition maintenance failed" in captured.err
    assert secret not in captured.err
