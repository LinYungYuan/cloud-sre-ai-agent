from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Protocol
from uuid import UUID

from sre_agent.domain.alerts.models import ClassificationStatus

ScopeField = Literal["team", "project", "environment", "service"]
SCOPE_FIELDS: tuple[ScopeField, ...] = (
    "team",
    "project",
    "environment",
    "service",
)


class ScopeResolver(Protocol):
    def resolve(self, field: ScopeField, label_value: str) -> UUID | None: ...


@dataclass(frozen=True, slots=True)
class AlertScope:
    team_id: UUID | None = None
    project_id: UUID | None = None
    environment_id: UUID | None = None
    service_id: UUID | None = None

    def get(self, field: ScopeField) -> UUID | None:
        return getattr(self, f"{field}_id")


@dataclass(frozen=True, slots=True)
class AlertMapping:
    id: UUID
    source_id: UUID
    priority: int
    created_at: datetime
    scope: AlertScope
    enabled: bool = True
    rule_uid: str | None = None
    folder: str | None = None
    required_labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "required_labels",
            MappingProxyType(dict(self.required_labels)),
        )


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    status: ClassificationStatus
    scope: AlertScope
    missing_fields: tuple[ScopeField, ...]
    matched_mapping_id: UUID | None = None


class AlertClassifier:
    def __init__(
        self,
        source_id: UUID,
        resolver: ScopeResolver,
        mappings: Iterable[AlertMapping],
    ) -> None:
        self._source_id = source_id
        self._resolver = resolver
        self._mappings = tuple(
            sorted(
                (mapping for mapping in mappings if mapping.enabled),
                key=lambda mapping: (
                    mapping.priority,
                    mapping.created_at,
                    mapping.id,
                ),
            )
        )

    def classify(
        self,
        labels: Mapping[str, object],
        rule_uid: str | None,
        folder: str | None,
    ) -> ClassificationResult:
        label_scope = AlertScope(
            **{
                f"{scope_field}_id": self._resolve_label(labels, scope_field)
                for scope_field in SCOPE_FIELDS
            }
        )
        label_missing = self._missing_fields(label_scope)
        if not label_missing:
            return ClassificationResult(
                status=ClassificationStatus.CLASSIFIED,
                scope=label_scope,
                missing_fields=(),
            )

        mapping = next(
            (
                candidate
                for candidate in self._mappings
                if self._matches(candidate, labels, rule_uid, folder)
            ),
            None,
        )
        if mapping is None:
            return ClassificationResult(
                status=ClassificationStatus.UNCLASSIFIED,
                scope=label_scope,
                missing_fields=label_missing,
            )

        merged_scope = AlertScope(
            **{
                f"{scope_field}_id": label_scope.get(scope_field)
                or mapping.scope.get(scope_field)
                for scope_field in SCOPE_FIELDS
            }
        )
        missing_fields = self._missing_fields(merged_scope)
        return ClassificationResult(
            status=(
                ClassificationStatus.UNCLASSIFIED
                if missing_fields
                else ClassificationStatus.CLASSIFIED
            ),
            scope=merged_scope,
            missing_fields=missing_fields,
            matched_mapping_id=mapping.id,
        )

    def _resolve_label(
        self,
        labels: Mapping[str, object],
        scope_field: ScopeField,
    ) -> UUID | None:
        label_value = (
            labels.get("cloud_scope_id")
            if scope_field == "project" and "cloud_scope_id" in labels
            else labels.get(scope_field)
        )
        if not isinstance(label_value, str):
            return None
        return self._resolver.resolve(scope_field, label_value)

    def _matches(
        self,
        mapping: AlertMapping,
        labels: Mapping[str, object],
        rule_uid: str | None,
        folder: str | None,
    ) -> bool:
        return (
            mapping.source_id == self._source_id
            and (mapping.rule_uid is None or mapping.rule_uid == rule_uid)
            and (mapping.folder is None or mapping.folder == folder)
            and all(labels.get(key) == value for key, value in mapping.required_labels.items())
        )

    @staticmethod
    def _missing_fields(scope: AlertScope) -> tuple[ScopeField, ...]:
        return tuple(field for field in SCOPE_FIELDS if scope.get(field) is None)
