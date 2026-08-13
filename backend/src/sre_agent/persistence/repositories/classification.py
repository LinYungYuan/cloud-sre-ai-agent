from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from sre_agent.application.alerts.ingest_grafana_alerts import Classifier
from sre_agent.domain.alerts.classification import (
    AlertClassifier,
    AlertMapping,
    AlertScope,
    ScopeField,
    ScopeResolver,
)


class UnknownGrafanaSourceError(RuntimeError):
    """Raised when no enabled source classifier exists for an authenticated source."""


class LoadedClassifierProvider:
    def __init__(self, classifiers: Mapping[UUID, Classifier]) -> None:
        self._classifiers = MappingProxyType(dict(classifiers))

    @property
    def source_ids(self) -> frozenset[UUID]:
        return frozenset(self._classifiers)

    def for_source(self, source_id: UUID) -> Classifier:
        try:
            return self._classifiers[source_id]
        except KeyError:
            raise UnknownGrafanaSourceError(
                "authenticated Grafana source is not enabled"
            ) from None


class LoadedScopeResolver(ScopeResolver):
    def __init__(self, records: Mapping[tuple[ScopeField, str], UUID]) -> None:
        self._records = MappingProxyType(dict(records))

    def resolve(self, field: ScopeField, label_value: str) -> UUID | None:
        return self._records.get((field, label_value))


async def load_classifier_provider(
    connection: AsyncConnection,
) -> LoadedClassifierProvider:
    source_rows = (
        (
            await connection.execute(
                text(
                    """
                SELECT source.id AS source_id,
                       team.id AS team_id,
                       team.name AS team_name,
                       project.id AS project_id,
                       project.name AS project_name,
                       environment.id AS environment_id,
                       environment.name AS environment_name,
                       service.id AS service_id,
                       service.name AS service_name
                FROM grafana_sources AS source
                JOIN projects AS project ON project.id = source.project_id
                JOIN teams AS team ON team.id = project.team_id
                JOIN environments AS environment
                  ON environment.id = source.environment_id
                 AND environment.project_id = source.project_id
                LEFT JOIN services AS service
                  ON service.environment_id = environment.id
                WHERE source.enabled
                ORDER BY source.id, service.id
                """
                )
            )
        )
        .mappings()
        .all()
    )
    mapping_rows = (
        (
            await connection.execute(
                text(
                    """
                SELECT id, source_id, matcher, priority,
                       team_id, project_id, environment_id, service_id,
                       enabled, created_at
                FROM classification_mappings
                WHERE enabled AND source_id IS NOT NULL
                ORDER BY source_id, priority, created_at, id
                """
                )
            )
        )
        .mappings()
        .all()
    )

    scope_records: dict[UUID, dict[tuple[ScopeField, str], UUID]] = {}
    for row in source_rows:
        source_id = row["source_id"]
        records = scope_records.setdefault(source_id, {})
        records[("team", row["team_name"])] = row["team_id"]
        records[("project", row["project_name"])] = row["project_id"]
        records[("environment", row["environment_name"])] = row["environment_id"]
        if row["service_id"] is not None:
            records[("service", row["service_name"])] = row["service_id"]

    mappings: dict[UUID, list[AlertMapping]] = {
        source_id: [] for source_id in scope_records
    }
    for row in mapping_rows:
        source_id = row["source_id"]
        if source_id not in mappings:
            continue
        mappings[source_id].append(_alert_mapping(dict(row)))

    return LoadedClassifierProvider(
        {
            source_id: AlertClassifier(
                source_id,
                LoadedScopeResolver(records),
                mappings[source_id],
            )
            for source_id, records in scope_records.items()
        }
    )


def _alert_mapping(row: dict[str, Any]) -> AlertMapping:
    matcher = row["matcher"]
    if not isinstance(matcher, dict):
        raise TypeError("classification matcher must be a JSON object")
    source_marker = matcher.get("sourceId")
    if source_marker is not None and str(row["source_id"]) != source_marker:
        raise ValueError("classification matcher sourceId does not match its row")
    labels = matcher.get("labels", {})
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        raise ValueError("classification matcher labels must be string pairs")
    required_labels = dict(labels)
    rule_name = matcher.get("ruleName")
    if rule_name is not None:
        if not isinstance(rule_name, str):
            raise ValueError("classification matcher ruleName must be text")
        required_labels["alertname"] = rule_name
    folder = matcher.get("folder")
    if folder is not None and not isinstance(folder, str):
        raise ValueError("classification matcher folder must be text")
    created_at = row["created_at"]
    if not isinstance(created_at, datetime):
        raise TypeError("classification mapping created_at must be a datetime")
    return AlertMapping(
        id=row["id"],
        source_id=row["source_id"],
        priority=row["priority"],
        created_at=created_at,
        scope=AlertScope(
            team_id=row["team_id"],
            project_id=row["project_id"],
            environment_id=row["environment_id"],
            service_id=row["service_id"],
        ),
        enabled=row["enabled"],
        folder=folder,
        required_labels=required_labels,
    )
