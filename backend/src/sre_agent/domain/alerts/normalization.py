import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sre_agent.domain.alerts.provider import Provider, ProviderDecision

_SAFE_PATH = re.compile(r"^(labels|annotations|values)(?:\.[A-Za-z0-9_.-]+)+$")
_SAFE_FORMATS = {
    "aws_account_id": re.compile(r"^[0-9]{12}$"),
    "gcp_project_id": re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"),
    "uuid": re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
    ),
    "non_blank": re.compile(r".*\S.*"),
}


class NormalizationStatus(StrEnum):
    NORMALIZED = "NORMALIZED"
    UNCLASSIFIED = "UNCLASSIFIED"
    VALIDATION_FAILED = "VALIDATION_FAILED"


class RuleCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    operator: Literal["exists", "equals", "prefix", "format"]
    value: str | None = None

    @model_validator(mode="after")
    def validate_declarative_condition(self) -> "RuleCondition":
        if _SAFE_PATH.fullmatch(self.path) is None:
            raise ValueError("condition path is not allowed")
        if self.operator == "exists" and self.value is not None:
            raise ValueError("exists does not accept a value")
        if self.operator in {"equals", "prefix"} and not self.value:
            raise ValueError("operator requires a non-empty value")
        if self.operator == "format" and self.value not in _SAFE_FORMATS:
            raise ValueError("format is not allowed")
        return self


class RuleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Provider
    resource_type: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    scope_path: str | None = None
    resource_id_path: str | None = None
    resource_name_path: str | None = None
    display_unit: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_output_paths(self) -> "RuleOutput":
        for path in (
            self.scope_path,
            self.resource_id_path,
            self.resource_name_path,
        ):
            if path is not None and _SAFE_PATH.fullmatch(path) is None:
                raise ValueError("output path is not allowed")
        return self


class NormalizationRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    source_id: UUID | None = None
    name: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    priority: int
    conditions: Annotated[tuple[RuleCondition, ...], Field(min_length=1)]
    output: RuleOutput
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class CanonicalBaseAlert:
    labels: Mapping[str, object]
    annotations: Mapping[str, object]
    values: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class AlertIssue:
    raw_text: str
    source: str = "grafana.annotations.AlertValues"
    content_type: str = "text/plain"
    untrusted: bool = True


@dataclass(frozen=True, slots=True)
class NormalizedResource:
    provider: Provider
    resource_type: str
    scope_id: str | None
    resource_id: str | None
    resource_name: str | None
    display_unit: str | None


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    status: NormalizationStatus
    resource: NormalizedResource | None
    rule_id: UUID | None
    rule_version: int | None
    warnings: tuple[str, ...]


class SafeRuleEngine:
    def __init__(self, rules: tuple[NormalizationRule, ...]) -> None:
        self._rules = tuple(rule for rule in rules if rule.enabled)

    def normalize(
        self,
        alert: CanonicalBaseAlert,
        provider: ProviderDecision,
    ) -> NormalizationResult:
        if provider.errors:
            return _result(
                NormalizationStatus.VALIDATION_FAILED,
                "provider_validation_failed",
            )

        matches = [rule for rule in self._rules if self._matches(rule, alert)]
        if not matches:
            return _result(
                NormalizationStatus.UNCLASSIFIED,
                "normalization_rule_not_found",
            )
        best_priority = min(rule.priority for rule in matches)
        best = [rule for rule in matches if rule.priority == best_priority]
        if len(best) != 1:
            return _result(
                NormalizationStatus.UNCLASSIFIED,
                "normalization_rule_conflict",
            )

        rule = best[0]
        if rule.output.provider is not provider.provider:
            return _result(
                NormalizationStatus.UNCLASSIFIED,
                "normalization_provider_conflict",
            )
        resource = self._resource(rule.output, alert)
        if resource is None:
            return _result(
                NormalizationStatus.UNCLASSIFIED,
                "normalization_output_invalid",
            )
        return NormalizationResult(
            status=NormalizationStatus.NORMALIZED,
            resource=resource,
            rule_id=rule.id,
            rule_version=rule.version,
            warnings=(),
        )

    @staticmethod
    def _matches(rule: NormalizationRule, alert: CanonicalBaseAlert) -> bool:
        for condition in rule.conditions:
            found, value = _resolve_path(alert, condition.path)
            if condition.operator == "exists":
                matched = found and value is not None
            elif condition.operator == "equals":
                matched = isinstance(value, str) and value == condition.value
            elif condition.operator == "prefix":
                matched = isinstance(value, str) and value.startswith(
                    condition.value or ""
                )
            else:
                pattern = _SAFE_FORMATS[condition.value or ""]
                matched = isinstance(value, str) and pattern.fullmatch(value) is not None
            if not matched:
                return False
        return True

    @staticmethod
    def _resource(
        output: RuleOutput,
        alert: CanonicalBaseAlert,
    ) -> NormalizedResource | None:
        extracted: list[str | None] = []
        for path in (
            output.scope_path,
            output.resource_id_path,
            output.resource_name_path,
        ):
            if path is None:
                extracted.append(None)
                continue
            found, value = _resolve_path(alert, path)
            if not found or not isinstance(value, str) or not value.strip():
                return None
            extracted.append(value)
        return NormalizedResource(
            provider=output.provider,
            resource_type=output.resource_type,
            scope_id=extracted[0],
            resource_id=extracted[1],
            resource_name=extracted[2],
            display_unit=output.display_unit,
        )


def _result(status: NormalizationStatus, warning: str) -> NormalizationResult:
    return NormalizationResult(
        status=status,
        resource=None,
        rule_id=None,
        rule_version=None,
        warnings=(warning,),
    )


def _resolve_path(alert: CanonicalBaseAlert, path: str) -> tuple[bool, object]:
    root_name, *parts = path.split(".")
    current: object = getattr(alert, root_name)
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current
