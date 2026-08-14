from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sre_rca_worker.agents.rca.models import IncidentContext
from sre_rca_worker.integrations.mcp.models import CapabilitySet, SpecialistKind


class RouteReasonCode(StrEnum):
    METRICS_AVAILABLE = "METRICS_AVAILABLE"
    METRICS_CAPABILITY_MISSING = "METRICS_CAPABILITY_MISSING"
    TRACE_AVAILABLE = "TRACE_AVAILABLE"
    TRACE_CAPABILITY_MISSING = "TRACE_CAPABILITY_MISSING"
    LOG_AVAILABLE = "LOG_AVAILABLE"
    LOG_CAPABILITY_MISSING = "LOG_CAPABILITY_MISSING"
    NON_GCP_OR_UNSAFE_SCOPE = "NON_GCP_OR_UNSAFE_SCOPE"


class RoutePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected: tuple[SpecialistKind, ...]
    reason_codes: tuple[RouteReasonCode, ...]


_ORDER = (SpecialistKind.METRICS, SpecialistKind.TRACE, SpecialistKind.LOG)
_AVAILABLE = {
    SpecialistKind.METRICS: RouteReasonCode.METRICS_AVAILABLE,
    SpecialistKind.TRACE: RouteReasonCode.TRACE_AVAILABLE,
    SpecialistKind.LOG: RouteReasonCode.LOG_AVAILABLE,
}
_MISSING = {
    SpecialistKind.METRICS: RouteReasonCode.METRICS_CAPABILITY_MISSING,
    SpecialistKind.TRACE: RouteReasonCode.TRACE_CAPABILITY_MISSING,
    SpecialistKind.LOG: RouteReasonCode.LOG_CAPABILITY_MISSING,
}


class RuleRouter:
    def route(self, context: IncidentContext, capabilities: CapabilitySet) -> RoutePlan:
        scope = context.scope
        if scope is None or scope.provider != "GCP" or not scope.safe:
            return RoutePlan(
                selected=(),
                reason_codes=(RouteReasonCode.NON_GCP_OR_UNSAFE_SCOPE,),
            )
        selected = tuple(kind for kind in _ORDER if capabilities.for_specialist(kind))
        reasons = tuple(
            _AVAILABLE[kind] if capabilities.for_specialist(kind) else _MISSING[kind]
            for kind in _ORDER
        )
        return RoutePlan(selected=selected, reason_codes=reasons)
