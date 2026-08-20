from fastapi import Request

from sre_agent.api.composition import ApplicationServices
from sre_agent.application.alerts.ingest_grafana_alerts import IngestGrafanaAlerts
from sre_agent.application.operator.read_models import (
    OperatorIdentityProvider,
    OperatorReadService,
)
from sre_agent.integrations.grafana.authenticator import GrafanaTokenAuthenticator


def _services(request: Request) -> ApplicationServices:
    services = getattr(request.app.state, "application_services", None)
    if not isinstance(services, ApplicationServices):
        raise TypeError("application services are not configured")
    return services


def get_grafana_authenticator(request: Request) -> GrafanaTokenAuthenticator:
    return _services(request).authenticator


def get_ingest_grafana_alerts(request: Request) -> IngestGrafanaAlerts:
    return _services(request).ingestion


def get_operator_identity_provider(request: Request) -> OperatorIdentityProvider:
    return _services(request).operator_identity_provider


def get_operator_read_service(request: Request) -> OperatorReadService:
    return _services(request).operator_reads
