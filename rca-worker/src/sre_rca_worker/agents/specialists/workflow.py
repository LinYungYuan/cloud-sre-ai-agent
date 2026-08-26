from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol, cast, get_args

from sre_rca_worker.agents.rca.models import (
    IncidentContext,
    SpecialistAnalysisBundle,
    SpecialistAnalysisResult,
    SpecialistFailure,
)
from sre_rca_worker.agents.rca.router import RuleRouter
from sre_rca_worker.agents.specialists.base import SpecialistRequest
from sre_rca_worker.agents.specialists.validator import (
    SpecialistAnalysisValidationError,
    SpecialistValidationCode,
)
from sre_rca_worker.domain.evidence.analysis import StableSpecialistCode
from sre_rca_worker.domain.evidence.chunking import McpPayloadTooLargeError
from sre_rca_worker.domain.evidence.errors import McpResultInvalidError
from sre_rca_worker.integrations.mcp.models import CapabilitySet, SpecialistKind

_ORDER = (SpecialistKind.METRICS, SpecialistKind.TRACE, SpecialistKind.LOG)
_STABLE_SPECIALIST_CODES: frozenset[str] = frozenset(get_args(StableSpecialistCode))
_VALIDATION_CODES: frozenset[str] = frozenset(get_args(SpecialistValidationCode))


class SpecialistBranchInvoker(Protocol):
    """Create and execute one fresh specialist branch attempt."""

    def __call__(
        self,
        request: SpecialistRequest,
        kind: SpecialistKind,
        deadline: datetime,
        /,
    ) -> Awaitable[SpecialistAnalysisResult]: ...


class SpecialistAnalysisWorkflow:
    """Route and concurrently execute persistence-agnostic specialist branches."""

    def __init__(
        self,
        branch_invoker: SpecialistBranchInvoker,
        router: RuleRouter | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._branch_invoker = branch_invoker
        self._router = router or RuleRouter()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run(
        self,
        context: IncidentContext,
        capabilities: CapabilitySet,
        *,
        deadline: datetime,
    ) -> SpecialistAnalysisBundle:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")

        results: dict[SpecialistKind, SpecialistAnalysisResult] = {}
        failures: dict[SpecialistKind, SpecialistFailure] = {
            failure.specialist: SpecialistFailure(
                specialist=failure.specialist,
                code=failure.code,
            )
            for failure in capabilities.discovery_failures
        }
        plan = self._router.route(context, capabilities)
        if not plan.selected:
            return SpecialistAnalysisBundle(
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
            for attempt in range(2):
                remaining = self._remaining_seconds(deadline)
                if remaining is None:
                    failures[kind] = self._failure(kind, "ANALYSIS_TIMEOUT")
                    return
                try:
                    async with asyncio.timeout(remaining):
                        result = await self._branch_invoker(request, kind, deadline)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    failures[kind] = self._failure(kind, "ANALYSIS_TIMEOUT")
                    return
                except (ConnectionError, OSError):
                    if attempt == 0:
                        continue
                    failures[kind] = self._failure(kind, "MCP_TRANSPORT")
                    return
                except McpPayloadTooLargeError:
                    failures[kind] = self._failure(kind, "MCP_PAYLOAD_TOO_LARGE")
                    return
                except McpResultInvalidError:
                    failures[kind] = self._failure(kind, "MCP_RESULT_INVALID")
                    return
                except SpecialistAnalysisValidationError as error:
                    failures[kind] = self._failure(
                        kind,
                        self._exception_code(error, allowed_codes=_VALIDATION_CODES),
                    )
                    return
                except Exception as error:  # noqa: BLE001 - safe failure boundary
                    code = self._exception_code(
                        error, allowed_codes=_STABLE_SPECIALIST_CODES
                    )
                    if code == "MCP_TRANSPORT" and attempt == 0:
                        continue
                    failures[kind] = self._failure(kind, code)
                    return

                if result.analysis.specialist is not kind:
                    failures[kind] = self._failure(kind, "ANALYSIS_SCHEMA_INVALID")
                    return
                results[kind] = result
                return

        async with asyncio.TaskGroup() as group:
            for kind in plan.selected:
                group.create_task(invoke(kind), name=f"analysis-{kind.value}")

        return SpecialistAnalysisBundle(
            results=tuple(results[kind] for kind in _ORDER if kind in results),
            failures=tuple(failures[kind] for kind in _ORDER if kind in failures),
        )

    def _remaining_seconds(self, deadline: datetime) -> float | None:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            return None
        remaining = (deadline - now).total_seconds()
        return remaining if remaining > 0 else None

    @staticmethod
    def _exception_code(
        error: Exception, *, allowed_codes: frozenset[str]
    ) -> StableSpecialistCode:
        code = getattr(error, "code", None)
        if (
            isinstance(code, str)
            and code in allowed_codes
            and code in _STABLE_SPECIALIST_CODES
        ):
            return cast(StableSpecialistCode, code)
        return "ANALYSIS_FAILED"

    @staticmethod
    def _failure(kind: SpecialistKind, code: StableSpecialistCode) -> SpecialistFailure:
        return SpecialistFailure(specialist=kind, code=code)


__all__ = [
    "SpecialistAnalysisWorkflow",
    "SpecialistBranchInvoker",
]
