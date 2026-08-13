from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Protocol

PARTITIONED_TABLES = frozenset(
    {
        "webhook_deliveries",
        "alert_events",
        "evidence_records",
        "incident_messages",
        "incident_timeline_events",
        "audit_events",
    }
)


class AsyncpgLikeConnection(Protocol):
    async def fetchval(self, query: str, *args: object) -> str: ...

    async def fetchrow(
        self, query: str, *args: object
    ) -> Mapping[str, object] | None: ...

    async def execute(self, query: str, *args: object) -> str: ...


class PartitionDriftError(RuntimeError):
    """Raised when a monthly partition name has unexpected catalog metadata."""


def _next_month(month: date) -> date:
    if month.month == 12:
        return date(month.year + 1, 1, 1)
    return date(month.year, month.month + 1, 1)


async def ensure_monthly_partitions(
    connection: AsyncpgLikeConnection, month: date
) -> None:
    """Create all allowlisted monthly partitions for ``month`` if absent."""
    start_date = month.replace(day=1)
    end_date = _next_month(start_date)
    start = datetime.combine(start_date, datetime.min.time(), UTC)
    end = datetime.combine(end_date, datetime.min.time(), UTC)

    for table_name in sorted(PARTITIONED_TABLES):
        partition_name = f"{table_name}_{start.year:04d}_{start.month:02d}"
        statement = await connection.fetchval(
            """
            SELECT format(
                'CREATE TABLE IF NOT EXISTS %I.%I PARTITION OF %I.%I '
                'FOR VALUES FROM (%L) TO (%L)',
                'public',
                $1::text,
                'public',
                $2::text,
                $3::timestamptz,
                $4::timestamptz
            )
            """,
            partition_name,
            table_name,
            start,
            end,
        )
        await connection.execute(statement)
        expected_bound = await connection.fetchval(
            """
            SELECT format(
                'FOR VALUES FROM (%L) TO (%L)',
                $1::timestamptz,
                $2::timestamptz
            )
            """,
            start,
            end,
        )
        metadata = await connection.fetchrow(
            """
            SELECT child.relispartition,
                   parent_namespace.nspname AS parent_schema,
                   parent.relname AS parent_name,
                   pg_get_expr(child.relpartbound, child.oid) AS partition_bound
            FROM pg_class AS child
            JOIN pg_namespace AS child_namespace
              ON child_namespace.oid = child.relnamespace
            LEFT JOIN pg_inherits AS inheritance
              ON inheritance.inhrelid = child.oid
            LEFT JOIN pg_class AS parent
              ON parent.oid = inheritance.inhparent
            LEFT JOIN pg_namespace AS parent_namespace
              ON parent_namespace.oid = parent.relnamespace
            WHERE child_namespace.nspname = 'public'
              AND child.relname = $1
            """,
            partition_name,
        )
        if (
            metadata is None
            or metadata["relispartition"] is not True
            or metadata["parent_schema"] != "public"
            or metadata["parent_name"] != table_name
            or metadata["partition_bound"] != expected_bound
        ):
            raise PartitionDriftError(
                f"partition drift detected for public.{partition_name}"
            )
