from __future__ import annotations

import importlib.util
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = ROOT / "migrations/versions/0002_adk_specialist_analysis.py"

STATUS_EXPRESSION = """status = ANY (ARRAY[
    'QUEUED'::text, 'RUNNING'::text, 'SUCCEEDED'::text,
    'FAILED'::text, 'SKIPPED'::text
])"""
FAILURE_EXPRESSION = """failure_code IS NULL OR failure_code = ANY (ARRAY[
    'DEADLINE_EXCEEDED'::text, 'MCP_TIMEOUT'::text, 'MCP_TRANSPORT'::text,
    'POLICY_DENIED'::text, 'VALIDATION_FAILED'::text, 'INTERNAL_ERROR'::text
])"""


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_0002_adk_specialist_analysis", MIGRATION_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CatalogResult:
    def __init__(self, candidates: Sequence[Mapping[str, Any]]) -> None:
        self._candidates = candidates

    def scalars(self):
        return (candidate["constraint_name"] for candidate in self._candidates)

    def mappings(self):
        return iter(self._candidates)


class _CatalogConnection:
    def __init__(self, candidates: Sequence[Mapping[str, Any]]) -> None:
        self._candidates = candidates

    def execute(self, *_args, **_kwargs) -> _CatalogResult:
        return _CatalogResult(self._candidates)


def _candidate(
    *,
    name: str,
    columns: Sequence[str],
    expression: str,
) -> dict[str, Any]:
    return {
        "constraint_name": name,
        "column_names": list(columns),
        "definition": f"CHECK ({expression})",
        "expression": expression,
    }


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(
            name="specialist_runs_status_check",
            columns=("status",),
            expression="status <> 'BROKEN'::text",
        ),
        _candidate(
            name="specialist_runs_status_check",
            columns=("status", "completed_at"),
            expression=STATUS_EXPRESSION,
        ),
    ],
    ids=("wrong-allowlist", "extra-column-invariant"),
)
def test_required_check_name_rejects_wrong_single_check(
    monkeypatch: pytest.MonkeyPatch,
    candidate: Mapping[str, Any],
) -> None:
    migration = _load_migration()
    monkeypatch.setattr(
        migration.op,
        "get_bind",
        lambda: _CatalogConnection((candidate,)),
    )

    with pytest.raises(RuntimeError, match="legacy allowlist CHECK"):
        migration._required_check_name("specialist_runs", "status")


@pytest.mark.parametrize(
    ("table_name", "column_name", "candidate"),
    [
        (
            "specialist_runs",
            "status",
            _candidate(
                name="specialist_runs_status_check",
                columns=("status",),
                expression=STATUS_EXPRESSION,
            ),
        ),
        (
            "rca_runs",
            "failure_code",
            _candidate(
                name="ck_rca_runs_failure_code",
                columns=("failure_code",),
                expression=FAILURE_EXPRESSION,
            ),
        ),
    ],
)
def test_required_check_name_accepts_exact_legacy_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    table_name: str,
    column_name: str,
    candidate: Mapping[str, Any],
) -> None:
    migration = _load_migration()
    monkeypatch.setattr(
        migration.op,
        "get_bind",
        lambda: _CatalogConnection((candidate,)),
    )

    assert (
        migration._required_check_name(table_name, column_name)
        == (candidate["constraint_name"])
    )


def test_downgrade_updates_only_specialist_only_failure_codes() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    rows = (
        (1, "NO_SAFE_MCP_CAPABILITY"),
        (2, "MCP_PAYLOAD_TOO_LARGE"),
        (3, "MCP_RESULT_INVALID"),
        (4, "ANALYSIS_TIMEOUT"),
        (5, "ANALYSIS_SCHEMA_INVALID"),
        (6, "ANALYSIS_UNKNOWN_EVIDENCE"),
        (7, "ANALYSIS_INPUT_TRUNCATED"),
        (8, "ANALYSIS_FAILED"),
        (9, "MCP_TIMEOUT"),
        (10, "POLICY_DENIED"),
        (11, None),
    )

    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE rca_runs (id INTEGER PRIMARY KEY, failure_code TEXT)")
        )
        connection.execute(sa.text("CREATE TABLE updated_rows (id INTEGER)"))
        connection.execute(
            sa.text(
                """CREATE TRIGGER audit_rca_runs_update
                   AFTER UPDATE ON rca_runs
                   BEGIN
                       INSERT INTO updated_rows (id) VALUES (NEW.id);
                   END"""
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO rca_runs (id, failure_code) VALUES (:id, :failure_code)"
            ),
            [{"id": row_id, "failure_code": code} for row_id, code in rows],
        )

        migration._downgrade_failure_codes(connection, "rca_runs")

        assert connection.execute(
            sa.text("SELECT id FROM updated_rows ORDER BY id")
        ).scalars().all() == list(range(1, 9))
        assert connection.execute(
            sa.text("SELECT failure_code FROM rca_runs ORDER BY id")
        ).scalars().all() == [
            "POLICY_DENIED",
            "VALIDATION_FAILED",
            "VALIDATION_FAILED",
            "DEADLINE_EXCEEDED",
            "VALIDATION_FAILED",
            "VALIDATION_FAILED",
            "VALIDATION_FAILED",
            "INTERNAL_ERROR",
            "MCP_TIMEOUT",
            "POLICY_DENIED",
            None,
        ]

    engine.dispose()
