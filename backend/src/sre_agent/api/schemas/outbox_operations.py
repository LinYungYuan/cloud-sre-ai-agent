from typing import Literal
from uuid import UUID

from sre_agent.api.schemas.operator import OperatorModel

OutboxPublishResult = Literal["PUBLISHED", "FAILED", "NO_OP"]
OutboxFailureCategory = Literal["INVALID_EVENT", "PUBLISH_ERROR"]


class OutboxRetryEventResponse(OperatorModel):
    event_id: UUID
    previous_status: str
    result: OutboxPublishResult
    failure_category: OutboxFailureCategory | None


class OutboxRetryBatchResponse(OperatorModel):
    selected: int
    published: int
    failed: int
    no_op: int
    failure_categories: list[OutboxFailureCategory]
