import os
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from sre_agent.domain.alerts.normalization import (
    CanonicalBaseAlert,
    NormalizationStatus,
)
from sre_agent.domain.alerts.provider import detect_provider
from sre_agent.persistence.repositories.normalization import (
    load_folder_scope_provider,
    load_normalization_rule_provider,
)

DATABASE_URL = os.getenv(
    "MIGRATION_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent",
)
TEAM_ID = UUID("11000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("12000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("13000000-0000-0000-0000-000000000001")
SERVICE_ID = UUID("14000000-0000-0000-0000-000000000001")
SOURCE_ONE = UUID("15000000-0000-0000-0000-000000000001")
SOURCE_TWO = UUID("15000000-0000-0000-0000-000000000002")


@pytest_asyncio.fixture
async def connection():
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.exec_driver_sql(
            "INSERT INTO teams (id, name) VALUES ($1, 'normalization-team')",
            (TEAM_ID,),
        )
        await connection.exec_driver_sql(
            "INSERT INTO projects (id, team_id, name) VALUES ($1, $2, 'normalization-project')",
            (PROJECT_ID, TEAM_ID),
        )
        await connection.exec_driver_sql(
            "INSERT INTO environments (id, project_id, name) VALUES ($1, $2, 'normalization-env')",
            (ENVIRONMENT_ID, PROJECT_ID),
        )
        await connection.exec_driver_sql(
            "INSERT INTO services (id, environment_id, name) VALUES ($1, $2, 'normalization-service')",
            (SERVICE_ID, ENVIRONMENT_ID),
        )
        for source_id, name in ((SOURCE_ONE, "source-one"), (SOURCE_TWO, "source-two")):
            await connection.exec_driver_sql(
                """
                INSERT INTO grafana_sources (id, project_id, environment_id, name)
                VALUES ($1, $2, $3, $4)
                """,
                (source_id, PROJECT_ID, ENVIRONMENT_ID, name),
            )
        try:
            yield connection
        finally:
            await transaction.rollback()
    await engine.dispose()


async def _insert_rule(
    connection: AsyncConnection,
    *,
    rule_id: UUID,
    source_id: UUID | None,
    name: str,
    priority: int,
    resource_type: str,
    enabled: bool = True,
    conditions: str = '[{"path":"labels.kind","operator":"equals","value":"database"}]',
) -> None:
    await connection.exec_driver_sql(
        """
        INSERT INTO normalization_rules (
            id, source_id, name, version, priority, provider,
            conditions, output, enabled, created_at
        ) VALUES (
            $1, $2, $3, 3, $4, 'AWS', $5::jsonb,
            jsonb_build_object('provider', 'AWS', 'resource_type', $6::text),
            $7, '2026-08-13T00:00:00Z'
        )
        """,
        (rule_id, source_id, name, priority, conditions, resource_type, enabled),
    )


@pytest.mark.asyncio
async def test_loader_builds_source_aware_immutable_rules_in_stable_priority_order(
    connection: AsyncConnection,
) -> None:
    await _insert_rule(
        connection,
        rule_id=UUID("16000000-0000-0000-0000-000000000001"),
        source_id=None,
        name="global",
        priority=20,
        resource_type="global_resource",
    )
    await _insert_rule(
        connection,
        rule_id=UUID("16000000-0000-0000-0000-000000000002"),
        source_id=SOURCE_ONE,
        name="source",
        priority=10,
        resource_type="source_resource",
    )
    await _insert_rule(
        connection,
        rule_id=UUID("16000000-0000-0000-0000-000000000003"),
        source_id=SOURCE_ONE,
        name="disabled",
        priority=1,
        resource_type="disabled_resource",
        enabled=False,
    )
    provider = await load_normalization_rule_provider(connection)
    alert = CanonicalBaseAlert(labels={"kind": "database"}, annotations={}, values={})

    source_result = provider.for_source(SOURCE_ONE).normalize(
        alert, detect_provider({})
    )
    global_result = provider.for_source(SOURCE_TWO).normalize(
        alert, detect_provider({})
    )

    assert {SOURCE_ONE, SOURCE_TWO} <= provider.source_ids
    assert source_result.status is NormalizationStatus.NORMALIZED
    assert source_result.rule_version == 3
    assert source_result.resource is not None
    assert source_result.resource.resource_type == "source_resource"
    assert global_result.resource is not None
    assert global_result.resource.resource_type == "global_resource"


@pytest.mark.asyncio
async def test_loader_returns_empty_engine_and_nullable_folder_scope(
    connection: AsyncConnection,
) -> None:
    rules = await load_normalization_rule_provider(connection)
    folders = await load_folder_scope_provider(connection)
    result = rules.for_source(SOURCE_ONE).normalize(
        CanonicalBaseAlert(labels={}, annotations={}, values={}), detect_provider({})
    )

    assert result.status is NormalizationStatus.UNCLASSIFIED
    assert folders.resolve(SOURCE_ONE, None).is_empty
    assert folders.resolve(SOURCE_ONE, "missing").is_empty

    await connection.exec_driver_sql(
        """
        INSERT INTO folder_scope_mappings (
            source_id, folder_code, team_id, project_id, environment_id, service_id
        ) VALUES ($1, 'COM-LX-BOA-01', $2, $3, $4, $5)
        """,
        (SOURCE_ONE, TEAM_ID, PROJECT_ID, ENVIRONMENT_ID, SERVICE_ID),
    )
    folders = await load_folder_scope_provider(connection)
    scope = folders.resolve(SOURCE_ONE, "COM-LX-BOA-01")

    assert (
        scope.team_id,
        scope.project_id,
        scope.environment_id,
        scope.service_id,
    ) == (
        TEAM_ID,
        PROJECT_ID,
        ENVIRONMENT_ID,
        SERVICE_ID,
    )


@pytest.mark.asyncio
async def test_invalid_persisted_rule_fails_loader(
    connection: AsyncConnection,
) -> None:
    await _insert_rule(
        connection,
        rule_id=UUID("16000000-0000-0000-0000-000000000004"),
        source_id=SOURCE_ONE,
        name="invalid",
        priority=1,
        resource_type="invalid_resource",
        conditions='[{"path":"labels.kind","operator":"exec","value":"x"}]',
    )

    with pytest.raises(ValidationError):
        await load_normalization_rule_provider(connection)
