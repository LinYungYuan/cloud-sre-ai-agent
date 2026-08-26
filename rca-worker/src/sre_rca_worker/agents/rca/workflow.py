from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime

from sre_rca_worker.agents.rca.models import (
    IncidentContext,
    InvestigationBundle,
    SpecialistFailure,
)
from sre_rca_worker.agents.rca.router import RuleRouter
from sre_rca_worker.agents.specialists.base import (
    McpResultInvalidError,
    Specialist,
    SpecialistRequest,
    SpecialistResult,
)
from sre_rca_worker.domain.evidence.chunking import McpPayloadTooLargeError
from sre_rca_worker.integrations.mcp.models import CapabilitySet, SpecialistKind

_ORDER = (SpecialistKind.METRICS, SpecialistKind.TRACE, SpecialistKind.LOG)


class RcaWorkflow:
    def __init__(
        self,
        specialists: Mapping[SpecialistKind, Specialist],
        router: RuleRouter | None = None,
    ) -> None:
        self._specialists = specialists
        self._router = router or RuleRouter()

    async def run(
        self,
        context: IncidentContext,
        capabilities: CapabilitySet,
        *,
        deadline: datetime,
    ) -> InvestigationBundle:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")
        if deadline <= datetime.now(UTC):
            raise TimeoutError("RCA deadline expired before routing")
        results: dict[SpecialistKind, SpecialistResult] = {}
        failures: dict[SpecialistKind, SpecialistFailure] = {
            failure.specialist: SpecialistFailure(
                specialist=failure.specialist,
                code=failure.code,
            )
            for failure in capabilities.discovery_failures
        }
        plan = self._router.route(context, capabilities)
        if not plan.selected:
            return InvestigationBundle(
                failures=tuple(failures[kind] for kind in _ORDER if kind in failures)
            )

        async def invoke(kind: SpecialistKind) -> None:
            request = SpecialistRequest(
                incident_id=context.incident_id,
                rca_run_id=context.rca_run_id,
                alert_issue=context.alert_issue,
                scope=context.scope,
                window_start=context.window_start,
                window_end=context.window_end,
                available_tools=capabilities.for_specialist(kind),
            )
            try:
                for attempt in range(2):
                    remaining = (deadline - datetime.now(UTC)).total_seconds()
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        async with asyncio.timeout(remaining):
                            results[kind] = await self._specialists[kind].run(
                                request, deadline
                            )
                        break
                    except (ConnectionError, OSError):
                        if attempt == 1:
                            raise
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                failures[kind] = SpecialistFailure(
                    specialist=kind, code="SPECIALIST_TIMEOUT"
                )
            except (ConnectionError, OSError):
                failures[kind] = SpecialistFailure(
                    specialist=kind, code="SPECIALIST_TRANSPORT"
                )
            except McpResultInvalidError:
                failures[kind] = SpecialistFailure(
                    specialist=kind, code="MCP_RESULT_INVALID"
                )
            except McpPayloadTooLargeError:
                failures[kind] = SpecialistFailure(
                    specialist=kind, code="MCP_PAYLOAD_TOO_LARGE"
                )
            except ValueError:
                failures[kind] = SpecialistFailure(
                    specialist=kind, code="SPECIALIST_VALIDATION"
                )
            except Exception:  # noqa: BLE001 - converted to a safe closed failure code
                failures[kind] = SpecialistFailure(
                    specialist=kind, code="SPECIALIST_FAILED"
                )

        async with asyncio.TaskGroup() as group:
            for kind in plan.selected:
                group.create_task(invoke(kind), name=f"rca-{kind.value}")

        return InvestigationBundle(
            results=tuple(results[kind] for kind in _ORDER if kind in results),
            failures=tuple(failures[kind] for kind in _ORDER if kind in failures),
        )
