from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Protocol

import asyncpg
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sre_agent.persistence.database import (
    AsyncpgLikeConnection,
    ensure_monthly_partitions,
)


class PartitionWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        extra="forbid",
        populate_by_name=True,
    )

    database_url: SecretStr

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        url = value.get_secret_value()
        if not url.startswith(("postgresql://", "postgresql+asyncpg://")):
            raise ValueError("DATABASE_URL must use PostgreSQL")
        if len(url.partition("://")[2]) == 0:
            raise ValueError("DATABASE_URL must not be empty")
        return value


class PartitionConnection(AsyncpgLikeConnection, Protocol):
    async def close(self) -> None: ...


ConnectionFactory = Callable[[str], Awaitable[PartitionConnection]]
Clock = Callable[[], datetime]


def _next_month(month: date) -> date:
    if month.month == 12:
        return date(month.year + 1, 1, 1)
    return date(month.year, month.month + 1, 1)


def partition_runway(current: date) -> tuple[date, date, date]:
    first = current.replace(day=1)
    second = _next_month(first)
    return first, second, _next_month(second)


async def _connect(database_url: str) -> PartitionConnection:
    return await asyncpg.connect(database_url)


def _now() -> datetime:
    return datetime.now(UTC)


async def maintain_partition_runway(
    settings: PartitionWorkerSettings,
    *,
    connection_factory: ConnectionFactory = _connect,
    clock: Clock = _now,
) -> None:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("partition worker clock must be timezone-aware")
    database_url = settings.database_url.get_secret_value().replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )
    connection = await connection_factory(database_url)
    try:
        for month in partition_runway(now.astimezone(UTC).date()):
            await ensure_monthly_partitions(connection, month)
    finally:
        await connection.close()


def main(
    *,
    run: Callable[[], Awaitable[None]] | None = None,
) -> int:
    async def configured_run() -> None:
        await maintain_partition_runway(
            PartitionWorkerSettings()  # pyright: ignore[reportCallIssue]
        )

    async def invoke() -> None:
        await (run() if run is not None else configured_run())

    try:
        asyncio.run(invoke())
    except Exception:  # noqa: BLE001 -- CLI is a safe process boundary
        print("partition maintenance failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
