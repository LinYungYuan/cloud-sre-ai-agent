from fastapi import FastAPI

from sre_agent.api.error_handlers import install_error_handlers
from sre_agent.api.middleware.correlation_id import CorrelationIdMiddleware
from sre_agent.api.routers.grafana_webhook import router as grafana_webhook_router


def create_app() -> FastAPI:
    application = FastAPI()
    application.add_middleware(CorrelationIdMiddleware)
    install_error_handlers(application)
    application.include_router(grafana_webhook_router)
    return application


app = create_app()
