from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from sre_agent.domain.alerts.cross_cloud import AlertValidationError

PROJECT_KEY = "resource.label.project_id"


class Provider(StrEnum):
    GCP = "GCP"
    AWS = "AWS"


@dataclass(frozen=True, slots=True)
class ProviderDecision:
    provider: Provider
    project_id: str | None
    errors: tuple[AlertValidationError, ...]


def detect_provider(labels: Mapping[str, object]) -> ProviderDecision:
    if PROJECT_KEY not in labels:
        return ProviderDecision(provider=Provider.AWS, project_id=None, errors=())

    value = labels[PROJECT_KEY]
    if isinstance(value, str) and value.strip():
        return ProviderDecision(
            provider=Provider.GCP,
            project_id=value.strip(),
            errors=(),
        )
    return ProviderDecision(
        provider=Provider.GCP,
        project_id=None,
        errors=(AlertValidationError(field=PROJECT_KEY, code="invalid_value"),),
    )
