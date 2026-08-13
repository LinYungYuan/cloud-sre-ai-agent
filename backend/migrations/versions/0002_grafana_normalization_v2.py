"""Add standard Grafana normalization and Incident identity v2."""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_grafana_normalization_v2"
down_revision: str | None = "0001_alert_incident_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE normalization_rules (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id UUID NULL REFERENCES grafana_sources(id),
            name TEXT NOT NULL,
            version INTEGER NOT NULL,
            priority INTEGER NOT NULL,
            provider TEXT NOT NULL,
            conditions JSONB NOT NULL,
            output JSONB NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT true,
            created_by UUID NULL REFERENCES subjects(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_normalization_rules_source_name_version
                UNIQUE NULLS NOT DISTINCT (source_id, name, version),
            CONSTRAINT ck_normalization_rules_version CHECK (version > 0),
            CONSTRAINT ck_normalization_rules_provider
                CHECK (provider IN ('GCP', 'AWS')),
            CONSTRAINT ck_normalization_rules_conditions_array
                CHECK (jsonb_typeof(conditions) = 'array'),
            CONSTRAINT ck_normalization_rules_output_object
                CHECK (jsonb_typeof(output) = 'object')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE folder_scope_mappings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id UUID NOT NULL REFERENCES grafana_sources(id),
            folder_code TEXT NOT NULL,
            team_id UUID NULL REFERENCES teams(id),
            project_id UUID NULL REFERENCES projects(id),
            environment_id UUID NULL REFERENCES environments(id),
            service_id UUID NULL REFERENCES services(id),
            enabled BOOLEAN NOT NULL DEFAULT true,
            created_by UUID NULL REFERENCES subjects(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_folder_scope_source_folder UNIQUE (source_id, folder_code),
            CONSTRAINT ck_folder_scope_nonempty
                CHECK (num_nonnulls(team_id, project_id, environment_id, service_id) >= 1),
            CONSTRAINT ck_folder_scope_team_environment_gap
                CHECK (team_id IS NULL OR environment_id IS NULL OR project_id IS NOT NULL),
            CONSTRAINT ck_folder_scope_project_service_gap
                CHECK (project_id IS NULL OR service_id IS NULL OR environment_id IS NOT NULL),
            CONSTRAINT ck_folder_scope_team_service_gap
                CHECK (team_id IS NULL OR service_id IS NULL OR
                       (project_id IS NOT NULL AND environment_id IS NOT NULL)),
            CONSTRAINT fk_folder_scope_team_project
                FOREIGN KEY (team_id, project_id) REFERENCES projects(team_id, id),
            CONSTRAINT fk_folder_scope_project_environment
                FOREIGN KEY (project_id, environment_id)
                REFERENCES environments(project_id, id),
            CONSTRAINT fk_folder_scope_environment_service
                FOREIGN KEY (environment_id, service_id)
                REFERENCES services(environment_id, id)
        )
        """
    )
    op.execute(
        """
        ALTER TABLE webhook_deliveries
            ADD COLUMN truncated_alerts INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN incomplete BOOLEAN NOT NULL DEFAULT false,
            ADD CONSTRAINT ck_webhook_deliveries_truncated_alerts
                CHECK (truncated_alerts >= 0)
        """
    )
    op.execute(
        """
        ALTER TABLE alert_events
            ADD COLUMN provider TEXT NULL,
            ADD COLUMN folder_code TEXT NULL,
            ADD COLUMN alert_name TEXT NULL,
            ADD COLUMN severity_raw TEXT NULL,
            ADD COLUMN severity_canonical TEXT NULL,
            ADD COLUMN issue JSONB NULL,
            ADD COLUMN resource JSONB NULL,
            ADD COLUMN normalization_status TEXT NOT NULL DEFAULT 'UNCLASSIFIED',
            ADD COLUMN normalization_rule_id UUID NULL
                REFERENCES normalization_rules(id),
            ADD COLUMN normalization_rule_version INTEGER NULL,
            ADD COLUMN normalization_warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD CONSTRAINT ck_alert_events_provider
                CHECK (provider IS NULL OR provider IN ('GCP', 'AWS')),
            ADD CONSTRAINT ck_alert_events_severity_canonical
                CHECK (severity_canonical IS NULL OR
                       severity_canonical IN ('SEV1', 'SEV3', 'UNMAPPED')),
            ADD CONSTRAINT ck_alert_events_issue_object
                CHECK (issue IS NULL OR jsonb_typeof(issue) = 'object'),
            ADD CONSTRAINT ck_alert_events_resource_object
                CHECK (resource IS NULL OR jsonb_typeof(resource) = 'object'),
            ADD CONSTRAINT ck_alert_events_normalization_status
                CHECK (normalization_status IN
                       ('NORMALIZED', 'UNCLASSIFIED', 'VALIDATION_FAILED')),
            ADD CONSTRAINT ck_alert_events_normalization_warnings_array
                CHECK (jsonb_typeof(normalization_warnings) = 'array'),
            ADD CONSTRAINT ck_alert_events_rule_reference
                CHECK ((normalization_rule_id IS NULL) =
                       (normalization_rule_version IS NULL))
        """
    )
    op.execute(
        """
        ALTER TABLE incidents
            ADD COLUMN identity_version INTEGER NOT NULL DEFAULT 1,
            ADD COLUMN provider TEXT NULL,
            ADD COLUMN folder_code TEXT NULL,
            ADD COLUMN alert_name TEXT NULL,
            ALTER COLUMN team_id DROP NOT NULL,
            ALTER COLUMN project_id DROP NOT NULL,
            ALTER COLUMN environment_id DROP NOT NULL,
            DROP CONSTRAINT incidents_severity_check,
            ADD CONSTRAINT ck_incidents_identity_version
                CHECK (identity_version IN (1, 2)),
            ADD CONSTRAINT ck_incidents_provider
                CHECK (provider IS NULL OR provider IN ('GCP', 'AWS')),
            ADD CONSTRAINT ck_incidents_severity_v2
                CHECK (severity IN ('SEV1', 'SEV2', 'SEV3', 'SEV4', 'UNMAPPED'))
        """
    )
    op.execute("DROP INDEX uq_incidents_active_identity")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_incidents_active_identity
        ON incidents (identity_version, identity_key)
        WHERE status IN ('OPEN', 'INVESTIGATING')
        """
    )
    op.execute(
        "CREATE INDEX ix_normalization_rules_lookup "
        "ON normalization_rules (source_id, enabled, priority)"
    )
    op.execute(
        "CREATE INDEX ix_folder_scope_mappings_lookup "
        "ON folder_scope_mappings (source_id, enabled, folder_code)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_folder_scope_mappings_lookup")
    op.execute("DROP INDEX ix_normalization_rules_lookup")
    op.execute("DROP INDEX uq_incidents_active_identity")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_incidents_active_identity
        ON incidents (identity_key)
        WHERE status IN ('OPEN', 'INVESTIGATING')
        """
    )
    op.execute(
        """
        ALTER TABLE incidents
            DROP CONSTRAINT ck_incidents_severity_v2,
            DROP CONSTRAINT ck_incidents_provider,
            DROP CONSTRAINT ck_incidents_identity_version,
            ADD CONSTRAINT incidents_severity_check
                CHECK (severity IN ('SEV1', 'SEV2', 'SEV3', 'SEV4')),
            ALTER COLUMN environment_id SET NOT NULL,
            ALTER COLUMN project_id SET NOT NULL,
            ALTER COLUMN team_id SET NOT NULL,
            DROP COLUMN alert_name,
            DROP COLUMN folder_code,
            DROP COLUMN provider,
            DROP COLUMN identity_version
        """
    )
    op.execute(
        """
        ALTER TABLE alert_events
            DROP COLUMN normalization_warnings,
            DROP COLUMN normalization_rule_version,
            DROP COLUMN normalization_rule_id,
            DROP COLUMN normalization_status,
            DROP COLUMN resource,
            DROP COLUMN issue,
            DROP COLUMN severity_canonical,
            DROP COLUMN severity_raw,
            DROP COLUMN alert_name,
            DROP COLUMN folder_code,
            DROP COLUMN provider
        """
    )
    op.execute(
        """
        ALTER TABLE webhook_deliveries
            DROP CONSTRAINT ck_webhook_deliveries_truncated_alerts,
            DROP COLUMN incomplete,
            DROP COLUMN truncated_alerts
        """
    )
    op.execute("DROP TABLE folder_scope_mappings")
    op.execute("DROP TABLE normalization_rules")
