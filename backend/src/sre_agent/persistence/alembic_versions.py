import re

from sqlalchemy.engine import Connection

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def reconcile_backend_version_tables(
    connection: Connection,
    *,
    schema: str = "public",
) -> None:
    """Safely adopt the Backend-specific Alembic version table name."""
    if _IDENTIFIER.fullmatch(schema) is None:
        raise ValueError("invalid PostgreSQL schema identifier")
    quoted_schema = f'"{schema}"'
    connection.exec_driver_sql(
        f"""
        DO $$
        DECLARE
            legacy_revision TEXT;
            backend_revision TEXT;
            legacy_count INTEGER;
            backend_count INTEGER;
        BEGIN
            IF to_regclass('{schema}.alembic_version') IS NOT NULL
               AND to_regclass('{schema}.alembic_version_backend') IS NULL THEN
                ALTER TABLE {quoted_schema}.alembic_version
                    RENAME TO alembic_version_backend;
            ELSIF to_regclass('{schema}.alembic_version') IS NOT NULL
               AND to_regclass('{schema}.alembic_version_backend') IS NOT NULL THEN
                SELECT count(*), min(version_num)
                  INTO legacy_count, legacy_revision
                  FROM {quoted_schema}.alembic_version;
                SELECT count(*), min(version_num)
                  INTO backend_count, backend_revision
                  FROM {quoted_schema}.alembic_version_backend;
                IF legacy_count <> 1 OR backend_count <> 1
                   OR legacy_revision IS DISTINCT FROM backend_revision THEN
                    RAISE EXCEPTION
                        'conflicting Alembic version tables; refusing automatic reconciliation';
                END IF;
                DROP TABLE {quoted_schema}.alembic_version;
            END IF;
        END $$
        """
    )
