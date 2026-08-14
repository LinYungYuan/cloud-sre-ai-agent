from sre_rca_worker.agents.specialists.base import McpSpecialist
from sre_rca_worker.integrations.mcp.models import SpecialistKind


class MetricsSpecialist(McpSpecialist):
    kind = SpecialistKind.METRICS
