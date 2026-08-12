"""Verify the public schema reference remains aligned with the migration."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
MIGRATION_PATH = (
    REPOSITORY_ROOT / "backend/migrations/versions/0001_alert_incident_schema.py"
)
DOCUMENTATION_PATH = REPOSITORY_ROOT / "docs/database/postgresql-schema.md"
PARTITION_EXAMPLES = {
    "webhook_deliveries_2031_12",
}
REQUIRED_INDEXES = {
    "uq_incidents_active_identity",
    "uq_rca_runs_active_incident",
    "uq_worker_jobs_run_type",
    "ix_webhook_deliveries_source_received",
    "ix_alert_events_source_fingerprint_observed",
    "ix_alert_instances_state_last_seen",
    "ix_incidents_scope_status_opened",
    "ix_evidence_records_run_observed",
    "ix_incident_messages_incident_created",
    "ix_incident_timeline_incident_occurred",
    "ix_audit_events_resource_occurred",
    "ix_worker_jobs_status_available",
    "ix_outbox_events_status_available",
}
REQUIRED_COLUMNS = {
    "alert_events": {
        "validation_status TEXT NOT NULL DEFAULT 'VALID'",
        "validation_errors JSONB NOT NULL DEFAULT '[]'::jsonb",
    },
    "incidents": {"identity_key TEXT NOT NULL"},
}


def _load_migration() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "alert_incident_schema", MIGRATION_PATH
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _document_sql_blocks(documentation: str) -> str:
    return "\n".join(re.findall(r"```sql\n(.*?)```", documentation, re.DOTALL))


def _documented_table_definitions(documentation: str) -> dict[str, str]:
    definitions: dict[str, str] = {}
    for sql_block in re.findall(r"```sql\n(.*?)```", documentation, re.DOTALL):
        matches = list(re.finditer(r"^CREATE TABLE ([a-z_]+)", sql_block, re.MULTILINE))
        for position, match in enumerate(matches):
            end = matches[position + 1].start() if position + 1 < len(matches) else None
            definitions[match.group(1)] = sql_block[match.start() : end]
    return definitions


def test_schema_reference_covers_every_migration_parent_table() -> None:
    """Removing a documented parent table must be detected before release."""
    assert DOCUMENTATION_PATH.is_file(), "schema reference document is missing"

    migration = _load_migration()
    migration_tables = {
        table_name
        for statement in migration.DDL
        if (match := re.match(r"^CREATE TABLE ([a-z_]+)", statement))
        for table_name in [match.group(1)]
    }
    documented_tables = {
        table_name
        for table_name in re.findall(
            r"^CREATE TABLE ([a-z_]+)",
            _document_sql_blocks(DOCUMENTATION_PATH.read_text(encoding="utf-8")),
            re.MULTILINE,
        )
        if table_name not in PARTITION_EXAMPLES
    }

    assert migration_tables <= documented_tables


def test_schema_reference_documents_partitions_and_required_indexes() -> None:
    """Removing partition syntax or an operational index must be detected."""
    assert DOCUMENTATION_PATH.is_file(), "schema reference document is missing"

    migration = _load_migration()
    documentation = DOCUMENTATION_PATH.read_text(encoding="utf-8")
    sql_blocks = _document_sql_blocks(documentation)
    table_definitions = _documented_table_definitions(documentation)

    for table_name in migration.PARTITIONED_TABLES:
        assert "PARTITION BY RANGE (partition_timestamp)" in table_definitions.get(
            table_name, ""
        )

    documented_indexes = set(
        re.findall(r"\b(?:UNIQUE )?INDEX ([a-z_]+)", sql_blocks)
    )
    assert REQUIRED_INDEXES <= documented_indexes


def test_schema_reference_matches_delivery_token_identifier_type() -> None:
    """The public DDL must preserve the authenticator's non-secret string ID."""
    migration = _load_migration()
    documentation = DOCUMENTATION_PATH.read_text(encoding="utf-8")
    documented_delivery = _documented_table_definitions(documentation)[
        "webhook_deliveries"
    ]
    migrated_delivery = next(
        statement
        for statement in migration.DDL
        if statement.startswith("CREATE TABLE webhook_deliveries")
    )

    assert "token_id TEXT" in migrated_delivery
    assert "token_id TEXT" in documented_delivery
    assert "token_id UUID" not in documented_delivery


def test_schema_reference_documents_delivery_validation_failure_status() -> None:
    """Accepted invalid deliveries must remain representable in public DDL."""
    migration = _load_migration()
    documentation = DOCUMENTATION_PATH.read_text(encoding="utf-8")
    documented_delivery = _documented_table_definitions(documentation)[
        "webhook_deliveries"
    ]
    migrated_delivery = next(
        statement
        for statement in migration.DDL
        if statement.startswith("CREATE TABLE webhook_deliveries")
    )

    expected_statuses = (
        "'RECEIVED', 'PROCESSED', 'DUPLICATE', 'VALIDATION_FAILED', "
        "'REJECTED', 'FAILED'"
    )
    assert expected_statuses in migrated_delivery
    assert expected_statuses in documented_delivery


def test_schema_reference_documents_validation_and_identity_columns() -> None:
    """Removing validation or identity columns from public DDL must be detected."""
    table_definitions = _documented_table_definitions(
        DOCUMENTATION_PATH.read_text(encoding="utf-8")
    )

    for table_name, required_columns in REQUIRED_COLUMNS.items():
        assert all(
            column in table_definitions.get(table_name, "")
            for column in required_columns
        )
