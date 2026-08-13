from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from sre_agent.domain.alerts.normalization import NormalizationRule, SafeRuleEngine
from sre_agent.persistence.repositories.incidents import IncidentScope


class NormalizationRuleProvider:
    def __init__(
        self,
        engines: Mapping[UUID, SafeRuleEngine],
        source_ids: frozenset[UUID],
    ) -> None:
        self._engines = MappingProxyType(dict(engines))
        self.source_ids = source_ids

    def for_source(self, source_id: UUID) -> SafeRuleEngine:
        return self._engines.get(source_id, SafeRuleEngine(()))


class FolderScopeProvider:
    def __init__(self, mappings: Mapping[tuple[UUID, str], IncidentScope]) -> None:
        self._mappings = MappingProxyType(dict(mappings))

    def resolve(self, source_id: UUID, folder_code: str | None) -> IncidentScope:
        if folder_code is None:
            return IncidentScope()
        return self._mappings.get((source_id, folder_code), IncidentScope())


async def load_normalization_rule_provider(
    connection: AsyncConnection,
) -> NormalizationRuleProvider:
    source_ids = frozenset(
        UUID(str(row.id))
        for row in (
            await connection.execute(
                text("SELECT id FROM grafana_sources WHERE enabled ORDER BY id")
            )
        )
    )
    rows = (
        await connection.execute(
            text(
                """
                SELECT id, source_id, name, version, priority, conditions, output,
                       enabled
                FROM normalization_rules
                WHERE enabled
                ORDER BY priority, created_at, id
                """
            )
        )
    ).mappings()
    global_rules: list[NormalizationRule] = []
    source_rules: dict[UUID, list[NormalizationRule]] = defaultdict(list)
    for row in rows:
        rule = NormalizationRule.model_validate(dict(row))
        if rule.source_id is None:
            global_rules.append(rule)
        else:
            if rule.source_id not in source_ids:
                raise ValueError("normalization rule references a disabled source")
            source_rules[rule.source_id].append(rule)
    return NormalizationRuleProvider(
        {
            source_id: SafeRuleEngine(tuple(source_rules[source_id] + global_rules))
            for source_id in source_ids
        },
        source_ids,
    )


async def load_folder_scope_provider(
    connection: AsyncConnection,
) -> FolderScopeProvider:
    rows = (
        await connection.execute(
            text(
                """
                SELECT mapping.source_id, mapping.folder_code, mapping.team_id,
                       mapping.project_id, mapping.environment_id, mapping.service_id
                FROM folder_scope_mappings AS mapping
                JOIN grafana_sources AS source ON source.id = mapping.source_id
                WHERE mapping.enabled AND source.enabled
                ORDER BY mapping.source_id, mapping.folder_code, mapping.id
                """
            )
        )
    ).mappings()
    return FolderScopeProvider(
        {
            (row["source_id"], row["folder_code"]): IncidentScope(
                team_id=row["team_id"],
                project_id=row["project_id"],
                environment_id=row["environment_id"],
                service_id=row["service_id"],
            )
            for row in rows
        }
    )
