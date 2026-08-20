import pytest

from sre_rca_worker.agents.rca.models import (
    InvestigationBundle,
    SpecialistFailure,
)
from sre_rca_worker.application.rca.processor import ProductionRcaProcessor
from sre_rca_worker.integrations.mcp.models import SpecialistKind


def test_total_transport_failure_requests_durable_job_retry() -> None:
    bundle = InvestigationBundle(
        failures=(
            SpecialistFailure(
                specialist=SpecialistKind.METRICS,
                code="SPECIALIST_TRANSPORT",
            ),
        )
    )

    with pytest.raises(ConnectionError, match="transient MCP failure"):
        ProductionRcaProcessor._raise_if_retryable_total_failure(bundle, 1)

    ProductionRcaProcessor._raise_if_retryable_total_failure(bundle, 3)


def test_partial_or_permanent_failure_does_not_request_durable_retry() -> None:
    permanent = InvestigationBundle(
        failures=(
            SpecialistFailure(
                specialist=SpecialistKind.METRICS,
                code="SPECIALIST_FAILED",
            ),
        )
    )
    ProductionRcaProcessor._raise_if_retryable_total_failure(permanent, 1)
