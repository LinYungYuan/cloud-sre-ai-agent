from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_schema_documentation_describes_worker_migration_order_and_data_loss() -> None:
    document = (ROOT / "docs/database/postgresql-schema.md").read_text()

    assert "Backend migration → RCA Worker migration" in document
    assert "alembic_version_backend" in document
    assert "alembic_version_rca_worker" in document
    assert "raw_result BYTEA" in document
    assert "downgrade" in document.lower()
    assert "無法還原" in document
