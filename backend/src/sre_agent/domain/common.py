from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator


class EvidenceRelation(StrEnum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    MISSING = "MISSING"


def require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)  # noqa: UP017


UtcTimestamp = Annotated[datetime, AfterValidator(require_aware_utc)]
