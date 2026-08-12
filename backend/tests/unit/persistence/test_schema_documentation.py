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
    "uq_rca_runs_active_incident",
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

    for table_name in migration.PARTITIONED_TABLES:
        assert re.search(
            rf"CREATE TABLE {table_name} \(.*?\) PARTITION BY RANGE "
            r"\(partition_timestamp\)",
            sql_blocks,
            re.DOTALL,
        )

    documented_indexes = set(
        re.findall(r"\b(?:UNIQUE )?INDEX ([a-z_]+)", sql_blocks)
    )
    assert REQUIRED_INDEXES <= documented_indexes
