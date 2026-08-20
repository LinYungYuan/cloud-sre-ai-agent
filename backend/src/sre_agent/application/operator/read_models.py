from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Never, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OperatorIdentity:
    external_id: str
    global_access: bool = False


class OperatorIdentityUnavailable(RuntimeError):
    """Raised when no production operator identity provider is configured."""


class OperatorResourceNotFound(LookupError):
    """Raised for missing and unauthorized resources alike."""


class OperatorCursorInvalid(ValueError):
    """Raised when an opaque pagination cursor is malformed."""


class OperatorIdentityProvider(Protocol):
    async def resolve(self, authorization: str | None) -> OperatorIdentity: ...


class OperatorReadService(Protocol):
    async def list_incidents(
        self,
        identity: OperatorIdentity,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]: ...

    async def get_incident(
        self, identity: OperatorIdentity, incident_id: UUID
    ) -> dict[str, Any]: ...

    async def get_alert(
        self, identity: OperatorIdentity, alert_id: UUID
    ) -> dict[str, Any]: ...

    async def list_rca_runs(
        self,
        identity: OperatorIdentity,
        incident_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]: ...

    async def get_rca_report(
        self, identity: OperatorIdentity, rca_run_id: UUID
    ) -> dict[str, Any]: ...


class UnavailableOperatorReadService:
    @staticmethod
    def _raise() -> Never:
        raise OperatorIdentityUnavailable("operator read service is unavailable")

    async def list_incidents(
        self,
        identity: OperatorIdentity,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        del identity, cursor, limit
        self._raise()

    async def get_incident(
        self, identity: OperatorIdentity, incident_id: UUID
    ) -> dict[str, Any]:
        del identity, incident_id
        self._raise()

    async def get_alert(
        self, identity: OperatorIdentity, alert_id: UUID
    ) -> dict[str, Any]:
        del identity, alert_id
        self._raise()

    async def list_rca_runs(
        self,
        identity: OperatorIdentity,
        incident_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        del identity, incident_id, cursor, limit
        self._raise()

    async def get_rca_report(
        self, identity: OperatorIdentity, rca_run_id: UUID
    ) -> dict[str, Any]:
        del identity, rca_run_id
        self._raise()


class UnavailableOperatorIdentityProvider:
    async def resolve(self, authorization: str | None) -> OperatorIdentity:
        del authorization
        raise OperatorIdentityUnavailable(
            "operator identity provider is not configured"
        )


class LocalOperatorIdentityProvider:
    def __init__(self, *, app_environment: str) -> None:
        if app_environment != "local":
            raise ValueError(
                "local operator identity is restricted to APP_ENVIRONMENT=local"
            )

    async def resolve(self, authorization: str | None) -> OperatorIdentity:
        del authorization
        return OperatorIdentity(external_id="local-sre", global_access=True)
