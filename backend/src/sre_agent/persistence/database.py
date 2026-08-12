from __future__ import annotations

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

    async def execute(self, query: str, *args: object) -> str: ...


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
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I '
                'FOR VALUES FROM (%L) TO (%L)',
                $1::text,
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
