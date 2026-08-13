import os
from datetime import UTC, datetime

import asyncpg
import pytest
from pydantic import SecretStr

from sre_agent.persistence.database import PARTITIONED_TABLES
from sre_agent.workers.partition_worker import (
    PartitionWorkerSettings,
    maintain_partition_runway,
)

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:55432/sre_agent",
).replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.mark.asyncio
async def test_worker_creates_december_to_february_runway_and_routes_old_boundary():
    settings = PartitionWorkerSettings(database_url=SecretStr(DATABASE_URL))

    await maintain_partition_runway(
        settings,
        clock=lambda: datetime(2040, 12, 17, 12, tzinfo=UTC),
    )

    connection = await asyncpg.connect(DATABASE_URL)
    try:
        expected = {
            f"{table}_{year:04d}_{month:02d}"
            for table in PARTITIONED_TABLES
            for year, month in ((2040, 12), (2041, 1), (2041, 2))
        }
        actual = set(
            await connection.fetchval(
                """
                SELECT array_agg(child.relname)
                FROM pg_inherits
                JOIN pg_class AS child ON child.oid = inhrelid
                WHERE child.relname = ANY($1::text[])
                """,
                list(expected),
            )
            or []
        )
        assert actual == expected

        routed_partition = await connection.fetchval(
            """
            INSERT INTO audit_events (
                partition_timestamp, occurred_at, action, resource_type, scope
            ) VALUES (
                '2041-02-01 00:00:00+00', '2041-02-01 00:00:00+00',
                'partition-boundary-test', 'partition', '{}'::jsonb
            )
            RETURNING tableoid::regclass::text
            """
        )
        assert routed_partition == "audit_events_2041_02"
    finally:
        await connection.execute(
            "DELETE FROM audit_events WHERE action = 'partition-boundary-test'"
        )
        await connection.close()
