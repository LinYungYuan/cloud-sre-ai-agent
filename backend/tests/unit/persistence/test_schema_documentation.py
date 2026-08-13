"""Verify the public schema reference remains aligned with the migration."""

import importlib.util
import re
from dataclasses import dataclass
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


def _normalize_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().rstrip(";"))


def _split_top_level(value: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    quoted = False
    index = 0
    while index < len(value):
        character = value[index]
        if character == "'":
            if quoted and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
            elif character == "," and depth == 0:
                parts.append(value[start:index])
                start = index + 1
        index += 1
    parts.append(value[start:])
    return tuple(part for part in parts if part.strip())


def _create_table_statements(sql: str) -> dict[str, str]:
    statements: dict[str, str] = {}
    pattern = re.compile(r"\bCREATE TABLE ([a-z_]+)\s*\(")
    for match in pattern.finditer(sql):
        depth = 1
        quoted = False
        index = match.end()
        while index < len(sql) and depth:
            character = sql[index]
            if character == "'":
                if quoted and index + 1 < len(sql) and sql[index + 1] == "'":
                    index += 2
                    continue
                quoted = not quoted
            elif not quoted:
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
            index += 1
        suffix_end = sql.find("\nCREATE ", index)
        if suffix_end == -1:
            suffix_end = len(sql)
        statements[match.group(1)] = sql[match.start() : suffix_end].strip()
    return statements


@dataclass(frozen=True)
class TableManifest:
    columns: tuple[tuple[str, str], ...]
    primary_keys: tuple[str, ...]
    uniques: tuple[str, ...]
    foreign_keys: tuple[str, ...]
    checks: tuple[str, ...]
    partition_clause: str | None


def _table_manifest(statement: str) -> TableManifest:
    opening = statement.index("(")
    depth = 1
    quoted = False
    index = opening + 1
    while index < len(statement) and depth:
        character = statement[index]
        if character == "'":
            if quoted and index + 1 < len(statement) and statement[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
        index += 1

    columns: list[tuple[str, str]] = []
    primary_keys: list[str] = []
    uniques: list[str] = []
    foreign_keys: list[str] = []
    checks: list[str] = []
    for raw_entry in _split_top_level(statement[opening + 1 : index - 1]):
        entry = _normalize_sql(raw_entry)
        if entry.startswith("PRIMARY KEY"):
            primary_keys.append(entry)
        elif entry.startswith("UNIQUE"):
            uniques.append(entry)
        elif entry.startswith("FOREIGN KEY"):
            foreign_keys.append(entry)
        elif entry.startswith("CHECK"):
            checks.append(entry)
        else:
            name, definition = entry.split(maxsplit=1)
            columns.append((name, definition))
            if " PRIMARY KEY" in f" {definition}":
                primary_keys.append(f"{name} PRIMARY KEY")
            if " UNIQUE" in f" {definition}":
                uniques.append(f"{name} UNIQUE")
            reference = re.search(r"REFERENCES [a-z_]+\s*\([^)]*\)", definition)
            if reference:
                foreign_keys.append(f"{name} {reference.group(0)}")
            check_start = definition.find("CHECK (")
            if check_start >= 0:
                checks.append(f"{name} {definition[check_start:]}")

    suffix = _normalize_sql(statement[index:])
    partition = re.search(r"PARTITION BY RANGE\s*\([^)]*\)", suffix)
    return TableManifest(
        columns=tuple(columns),
        primary_keys=tuple(sorted(primary_keys)),
        uniques=tuple(sorted(uniques)),
        foreign_keys=tuple(sorted(foreign_keys)),
        checks=tuple(sorted(checks)),
        partition_clause=partition.group(0) if partition else None,
    )


def _index_manifest(sql: str) -> dict[str, tuple[bool, str, str, str | None]]:
    pattern = re.compile(
        r"CREATE\s+(UNIQUE\s+)?INDEX\s+([a-z_]+)\s+ON\s+([a-z_]+)\s*"
        r"(\([^;]*?\))(?:\s+WHERE\s+([^;\n]*(?:\n(?!CREATE)[^;\n]*)*))?",
        re.IGNORECASE,
    )
    indexes: dict[str, tuple[bool, str, str, str | None]] = {}
    for match in pattern.finditer(sql):
        predicate = _normalize_sql(match.group(5)) if match.group(5) else None
        indexes[match.group(2)] = (
            bool(match.group(1)),
            match.group(3),
            _normalize_sql(match.group(4)),
            predicate,
        )
    return indexes


def test_schema_reference_canonical_manifest_matches_migration() -> None:
    migration = _load_migration()
    migration_sql = "\n".join(migration.DDL)
    documentation_sql = _document_sql_blocks(
        DOCUMENTATION_PATH.read_text(encoding="utf-8")
    )
    migration_tables = _create_table_statements(migration_sql)
    documented_tables = _create_table_statements(documentation_sql)

    assert set(migration_tables) <= set(documented_tables)
    assert {
        name: _table_manifest(statement) for name, statement in migration_tables.items()
    } == {name: _table_manifest(documented_tables[name]) for name in migration_tables}
    assert _index_manifest(migration_sql) == _index_manifest(documentation_sql)


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

    documented_indexes = set(re.findall(r"\b(?:UNIQUE )?INDEX ([a-z_]+)", sql_blocks))
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
