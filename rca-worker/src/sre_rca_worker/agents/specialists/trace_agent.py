from sre_rca_worker.agents.specialists.base import McpSpecialist
from sre_rca_worker.domain.evidence.trace_waterfall import normalize_trace_evidence
from sre_rca_worker.integrations.mcp.models import SpecialistKind


class TraceSpecialist(McpSpecialist):
    kind = SpecialistKind.TRACE

    def _normalize_structured(self, structured, request):  # type: ignore[no-untyped-def]
        return normalize_trace_evidence(structured, alert_issue=request.alert_issue)
