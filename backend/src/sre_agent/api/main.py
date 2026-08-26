from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from sre_agent.api.composition import (
    ResourceFactory,
    compose_services,
    production_resources,
)
from sre_agent.api.error_handlers import install_error_handlers
from sre_agent.api.middleware.correlation_id import CorrelationIdMiddleware
from sre_agent.api.routers.grafana_webhook import router as grafana_webhook_router
from sre_agent.api.routers.health import router as health_router
from sre_agent.api.routers.operator_incidents import router as operator_router
from sre_agent.config.env_files import resolve_backend_env_file
from sre_agent.config.settings import Settings


def _load_settings() -> Settings:
    return Settings(  # pyright: ignore[reportCallIssue]
        _env_file=resolve_backend_env_file()
    )


def create_app(
    *,
    settings_factory: Callable[[], Settings] = _load_settings,
    resource_factory: ResourceFactory = production_resources,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        settings = settings_factory()
        async with resource_factory(settings) as resources:
            application.state.application_services = compose_services(
                settings,
                resources,
            )
            try:
                yield
            finally:
                del application.state.application_services

    application = FastAPI(lifespan=lifespan)
    application.add_middleware(CorrelationIdMiddleware)
    install_error_handlers(application)
    application.include_router(health_router)
    application.include_router(grafana_webhook_router)
    application.include_router(operator_router)
    return application


app = create_app()
