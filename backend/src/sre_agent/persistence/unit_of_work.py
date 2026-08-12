from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sre_agent.persistence.repositories.alerts import (
    AlertRepository,
    SqlAlchemyAlertRepository,
)
from sre_agent.persistence.repositories.incidents import (
    IncidentRepository,
    SqlAlchemyIncidentRepository,
)
from sre_agent.persistence.repositories.jobs import (
    JobRepository,
    SqlAlchemyJobRepository,
)


class UnitOfWork(Protocol):
    alerts: AlertRepository
    incidents: IncidentRepository
    jobs: JobRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class SqlAlchemyUnitOfWork:
    alerts: AlertRepository
    incidents: IncidentRepository
    jobs: JobRepository

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        jobs_repository_factory: Callable[[AsyncSession], JobRepository] = (
            SqlAlchemyJobRepository
        ),
    ) -> None:
        self._session_factory = session_factory
        self._jobs_repository_factory = jobs_repository_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        session = self._session_factory()
        self._session = session
        await session.begin()
        self.alerts = SqlAlchemyAlertRepository(session)
        self.incidents = SqlAlchemyIncidentRepository(session)
        self.jobs = self._jobs_repository_factory(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()
