from fastapi import Request

from sre_agent.api.composition import ApplicationServices, ReadinessCheck
from sre_agent.application.alerts.ingest_grafana_alerts import IngestGrafanaAlerts
from sre_agent.application.operator.read_models import (
    OperatorIdentityProvider,
    OperatorIdentityUnavailable,
    OperatorReadService,
)
from sre_agent.application.outbox.recover_events import OutboxRecoveryService
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


def get_outbox_recovery_service(request: Request) -> OutboxRecoveryService:
    service = _services(request).outbox_recovery_service
    if service is None:
        raise OperatorIdentityUnavailable("outbox recovery service is unavailable")
    return service


def get_readiness_check(request: Request) -> ReadinessCheck:
    return _services(request).readiness_check
