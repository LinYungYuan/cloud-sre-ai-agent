from sre_agent.application.alerts.ingest_grafana_alerts import IngestGrafanaAlerts
from sre_agent.integrations.grafana.authenticator import GrafanaTokenAuthenticator


def get_grafana_authenticator() -> GrafanaTokenAuthenticator:
    raise RuntimeError("Grafana authenticator dependency is not configured")


def get_ingest_grafana_alerts() -> IngestGrafanaAlerts:
    raise RuntimeError("Grafana ingestion dependency is not configured")
