"""Replace partitioned runtime relations with ordinary UUID-keyed tables.

This revision is intended for a maintenance window with writers stopped.  It
retains the partitioned parents (and their child partitions) under fixed
legacy names so operators have a rollback *source*, but it deliberately does
not implement a downgrade: writes accepted by the ordinary tables cannot be
mapped losslessly back to a partitioned layout.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_non_partition_runtime_tables"
down_revision: str | None = "0002_grafana_normalization_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CANONICAL_TABLES = (
    "webhook_deliveries",
    "alert_events",
    "evidence_records",
    "incident_messages",
    "incident_timeline_events",
    "audit_events",
)

LEGACY_TABLES = {
    table_name: f"{table_name}__partitioned_legacy_0003"
    for table_name in CANONICAL_TABLES
}

REPLACEMENT_DDL = (
    """CREATE TABLE webhook_deliveries_new (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        received_at TIMESTAMPTZ NOT NULL,
        source_id UUID NOT NULL REFERENCES grafana_sources(id),
        token_id TEXT,
        body_hash TEXT NOT NULL,
        raw_body BYTEA NOT NULL,
        raw_payload JSONB NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('RECEIVED', 'PROCESSED', 'DUPLICATE', 'VALIDATION_FAILED', 'REJECTED', 'FAILED')),
        processed_at TIMESTAMPTZ,
        error_message TEXT,
        truncated_alerts INTEGER NOT NULL DEFAULT 0,
        incomplete BOOLEAN NOT NULL DEFAULT false,
        CONSTRAINT ck_webhook_deliveries_truncated_alerts CHECK (truncated_alerts >= 0)
    )""",
    """CREATE TABLE alert_events_new (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        observed_at TIMESTAMPTZ NOT NULL,
        source_id UUID NOT NULL REFERENCES grafana_sources(id),
        delivery_id UUID NOT NULL REFERENCES webhook_deliveries_new(id),
        fingerprint TEXT NOT NULL,
        alert_state TEXT NOT NULL CHECK (alert_state IN ('FIRING', 'RESOLVED')),
        validation_status TEXT NOT NULL DEFAULT 'VALID'
            CHECK (validation_status IN ('VALID', 'VALIDATION_FAILED')),
        validation_errors JSONB NOT NULL DEFAULT '[]'::jsonb,
        starts_at TIMESTAMPTZ,
        ends_at TIMESTAMPTZ,
        labels JSONB NOT NULL DEFAULT '{}'::jsonb,
        annotations JSONB NOT NULL DEFAULT '{}'::jsonb,
        raw_payload JSONB NOT NULL,
        provider TEXT NULL,
        folder_code TEXT NULL,
        alert_name TEXT NULL,
        severity_raw TEXT NULL,
        severity_canonical TEXT NULL,
        issue JSONB NULL,
        resource JSONB NULL,
        normalization_status TEXT NOT NULL DEFAULT 'UNCLASSIFIED',
        normalization_rule_id UUID NULL REFERENCES normalization_rules(id),
        normalization_rule_version INTEGER NULL,
        normalization_warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
        CONSTRAINT ck_alert_events_provider CHECK (provider IS NULL OR provider IN ('GCP', 'AWS')),
        CONSTRAINT ck_alert_events_severity_canonical CHECK (severity_canonical IS NULL OR severity_canonical IN ('SEV1', 'SEV3', 'UNMAPPED')),
        CONSTRAINT ck_alert_events_issue_object CHECK (issue IS NULL OR jsonb_typeof(issue) = 'object'),
        CONSTRAINT ck_alert_events_resource_object CHECK (resource IS NULL OR jsonb_typeof(resource) = 'object'),
        CONSTRAINT ck_alert_events_normalization_status CHECK (normalization_status IN ('NORMALIZED', 'UNCLASSIFIED', 'VALIDATION_FAILED')),
        CONSTRAINT ck_alert_events_normalization_warnings_array CHECK (jsonb_typeof(normalization_warnings) = 'array'),
        CONSTRAINT ck_alert_events_rule_reference CHECK ((normalization_rule_id IS NULL) = (normalization_rule_version IS NULL))
    )""",
    """CREATE TABLE evidence_records_new (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        observed_at TIMESTAMPTZ NOT NULL,
        rca_run_id UUID NOT NULL REFERENCES rca_runs(id),
        specialist_run_id UUID REFERENCES specialist_runs(id),
        evidence_type TEXT NOT NULL,
        source_agent TEXT NOT NULL,
        source_endpoint TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        team_id UUID REFERENCES teams(id),
        project_id UUID REFERENCES projects(id),
        environment_id UUID REFERENCES environments(id),
        service_id UUID REFERENCES services(id),
        time_window_start TIMESTAMPTZ NOT NULL,
        time_window_end TIMESTAMPTZ NOT NULL,
        structured_data JSONB NOT NULL,
        raw_result_reference TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        CHECK (time_window_end >= time_window_start),
        CHECK (team_id IS NULL OR environment_id IS NULL OR project_id IS NOT NULL),
        CHECK (project_id IS NULL OR service_id IS NULL OR environment_id IS NOT NULL),
        CHECK (team_id IS NULL OR service_id IS NULL OR (project_id IS NOT NULL AND environment_id IS NOT NULL)),
        FOREIGN KEY (team_id, project_id) REFERENCES projects(team_id, id),
        FOREIGN KEY (project_id, environment_id) REFERENCES environments(project_id, id),
        FOREIGN KEY (environment_id, service_id) REFERENCES services(environment_id, id)
    )""",
    """CREATE TABLE incident_messages_new (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        incident_id UUID NOT NULL REFERENCES incidents(id),
        role TEXT NOT NULL CHECK (role IN ('USER', 'AGENT', 'SYSTEM')),
        subject_id UUID REFERENCES subjects(id),
        rca_run_id UUID REFERENCES rca_runs(id),
        content TEXT NOT NULL,
        evidence_references JSONB NOT NULL DEFAULT '[]'::jsonb
    )""",
    """CREATE TABLE incident_timeline_events_new (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        occurred_at TIMESTAMPTZ NOT NULL,
        incident_id UUID NOT NULL REFERENCES incidents(id),
        event_type TEXT NOT NULL,
        actor_id UUID REFERENCES subjects(id),
        payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )""",
    """CREATE TABLE audit_events_new (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        actor_id UUID REFERENCES subjects(id),
        action TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        resource_id UUID,
        scope JSONB NOT NULL DEFAULT '{}'::jsonb,
        before_value JSONB,
        after_value JSONB
    )""",
)

DEPENDENT_DDL = (
    """CREATE TABLE ingestion_dedup_keys_new (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        source_id UUID NOT NULL REFERENCES grafana_sources(id),
        dedup_key TEXT NOT NULL,
        delivery_id UUID NOT NULL REFERENCES webhook_deliveries_new(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (source_id, dedup_key)
    )""",
    """CREATE TABLE alert_instances_new (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        source_id UUID NOT NULL REFERENCES grafana_sources(id),
        fingerprint TEXT NOT NULL,
        latest_event_id UUID NOT NULL REFERENCES alert_events_new(id),
        state TEXT NOT NULL CHECK (state IN ('FIRING', 'RESOLVED')),
        labels JSONB NOT NULL DEFAULT '{}'::jsonb,
        annotations JSONB NOT NULL DEFAULT '{}'::jsonb,
        first_seen_at TIMESTAMPTZ NOT NULL,
        last_seen_at TIMESTAMPTZ NOT NULL,
        resolved_at TIMESTAMPTZ,
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        UNIQUE (source_id, fingerprint)
    )""",
    """CREATE TABLE incident_alerts_new (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        incident_id UUID NOT NULL REFERENCES incidents(id),
        alert_event_id UUID NOT NULL REFERENCES alert_events_new(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (incident_id, alert_event_id)
    )""",
    """CREATE TABLE hypothesis_evidence_new (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        hypothesis_id UUID NOT NULL REFERENCES rca_hypotheses(id),
        evidence_id UUID NOT NULL REFERENCES evidence_records_new(id),
        relation TEXT NOT NULL CHECK (relation IN ('SUPPORTS', 'CONTRADICTS', 'MISSING')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (hypothesis_id, evidence_id, relation)
    )""",
)

COPY_STATEMENTS = (
    """INSERT INTO webhook_deliveries_new (
        id, received_at, source_id, token_id, body_hash, raw_body, raw_payload,
        status, processed_at, error_message, truncated_alerts, incomplete
    ) SELECT
        id, received_at, source_id, token_id, body_hash, raw_body, raw_payload,
        status, processed_at, error_message, truncated_alerts, incomplete
    FROM webhook_deliveries""",
    """INSERT INTO alert_events_new (
        id, observed_at, source_id, delivery_id, fingerprint, alert_state,
        validation_status, validation_errors, starts_at, ends_at, labels,
        annotations, raw_payload, provider, folder_code, alert_name, severity_raw,
        severity_canonical, issue, resource, normalization_status,
        normalization_rule_id, normalization_rule_version, normalization_warnings
    ) SELECT
        id, observed_at, source_id, delivery_id, fingerprint, alert_state,
        validation_status, validation_errors, starts_at, ends_at, labels,
        annotations, raw_payload, provider, folder_code, alert_name, severity_raw,
        severity_canonical, issue, resource, normalization_status,
        normalization_rule_id, normalization_rule_version, normalization_warnings
    FROM alert_events""",
    """INSERT INTO evidence_records_new (
        id, observed_at, rca_run_id, specialist_run_id, evidence_type, source_agent,
        source_endpoint, tool_name, team_id, project_id, environment_id, service_id,
        time_window_start, time_window_end, structured_data, raw_result_reference,
        content_hash
    ) SELECT
        id, observed_at, rca_run_id, specialist_run_id, evidence_type, source_agent,
        source_endpoint, tool_name, team_id, project_id, environment_id, service_id,
        time_window_start, time_window_end, structured_data, raw_result_reference,
        content_hash
    FROM evidence_records""",
    """INSERT INTO incident_messages_new (
        id, created_at, incident_id, role, subject_id, rca_run_id, content,
        evidence_references
    ) SELECT
        id, created_at, incident_id, role, subject_id, rca_run_id, content,
        evidence_references
    FROM incident_messages""",
    """INSERT INTO incident_timeline_events_new (
        id, occurred_at, incident_id, event_type, actor_id, payload
    ) SELECT id, occurred_at, incident_id, event_type, actor_id, payload
    FROM incident_timeline_events""",
    """INSERT INTO audit_events_new (
        id, occurred_at, actor_id, action, resource_type, resource_id, scope,
        before_value, after_value
    ) SELECT
        id, occurred_at, actor_id, action, resource_type, resource_id, scope,
        before_value, after_value
    FROM audit_events""",
    """INSERT INTO ingestion_dedup_keys_new (
        id, source_id, dedup_key, delivery_id, created_at
    ) SELECT id, source_id, dedup_key, delivery_id, created_at
    FROM ingestion_dedup_keys""",
    """INSERT INTO alert_instances_new (
        id, source_id, fingerprint, latest_event_id, state, labels, annotations,
        first_seen_at, last_seen_at, resolved_at, version
    ) SELECT
        id, source_id, fingerprint, latest_event_id, state, labels, annotations,
        first_seen_at, last_seen_at, resolved_at, version
    FROM alert_instances""",
    """INSERT INTO incident_alerts_new (
        id, incident_id, alert_event_id, created_at
    ) SELECT id, incident_id, alert_event_id, created_at
    FROM incident_alerts""",
    """INSERT INTO hypothesis_evidence_new (
        id, hypothesis_id, evidence_id, relation, created_at
    ) SELECT id, hypothesis_id, evidence_id, relation, created_at
    FROM hypothesis_evidence""",
)


def _assert_no_duplicate_uuids() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            table_name TEXT;
            duplicate_count BIGINT;
        BEGIN
            FOREACH table_name IN ARRAY ARRAY[
                'webhook_deliveries', 'alert_events', 'evidence_records',
                'incident_messages', 'incident_timeline_events', 'audit_events'
            ]
            LOOP
                EXECUTE format(
                    'SELECT count(*) FROM (SELECT id FROM public.%I GROUP BY id HAVING count(*) > 1) AS duplicate_ids',
                    table_name
                ) INTO duplicate_count;
                IF duplicate_count > 0 THEN
                    RAISE EXCEPTION 'duplicate UUID precheck failed for %', table_name;
                END IF;
            END LOOP;
        END $$;
        """
    )


def _assert_copy_counts_and_uuids() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            source_table TEXT;
            source_count BIGINT;
            replacement_count BIGINT;
            duplicate_count BIGINT;
        BEGIN
            FOREACH source_table IN ARRAY ARRAY[
                'webhook_deliveries', 'alert_events', 'evidence_records',
                'incident_messages', 'incident_timeline_events', 'audit_events',
                'ingestion_dedup_keys', 'alert_instances', 'incident_alerts',
                'hypothesis_evidence'
            ]
            LOOP
                EXECUTE format('SELECT count(*) FROM public.%I', source_table)
                    INTO source_count;
                EXECUTE format('SELECT count(*) FROM public.%I_new', source_table)
                    INTO replacement_count;
                IF source_count <> replacement_count THEN
                    RAISE EXCEPTION 'copy count mismatch for %: source %, replacement %',
                        source_table, source_count, replacement_count;
                END IF;
                EXECUTE format(
                    'SELECT count(*) FROM (SELECT id FROM public.%I_new GROUP BY id HAVING count(*) > 1) AS duplicate_ids',
                    source_table
                ) INTO duplicate_count;
                IF duplicate_count > 0 THEN
                    RAISE EXCEPTION 'duplicate UUID copied into %_new', source_table;
                END IF;
            END LOOP;
        END $$;
        """
    )


def _rename_legacy_indexes() -> None:
    # Index names are schema-global.  Free canonical names before rebuilding them
    # on the replacements; the renamed indexes remain attached to legacy parents.
    for old_name, legacy_name in (
        (
            "ix_webhook_deliveries_source_received",
            "ix_webhook_deliveries_source_received_legacy_0003",
        ),
        (
            "ix_alert_events_source_fingerprint_observed",
            "ix_alert_events_source_fingerprint_observed_legacy_0003",
        ),
        (
            "ix_evidence_records_run_observed",
            "ix_evidence_records_run_observed_legacy_0003",
        ),
        (
            "ix_incident_messages_incident_created",
            "ix_incident_messages_incident_created_legacy_0003",
        ),
        (
            "ix_incident_timeline_incident_occurred",
            "ix_incident_timeline_incident_occurred_legacy_0003",
        ),
        (
            "ix_audit_events_resource_occurred",
            "ix_audit_events_resource_occurred_legacy_0003",
        ),
    ):
        op.execute(f"ALTER INDEX {old_name} RENAME TO {legacy_name}")


def _rebuild_indexes() -> None:
    for statement in (
        "CREATE INDEX ix_webhook_deliveries_source_received ON webhook_deliveries (source_id, received_at)",
        "CREATE INDEX ix_alert_events_source_fingerprint_observed ON alert_events (source_id, fingerprint, observed_at)",
        "CREATE INDEX ix_alert_instances_state_last_seen ON alert_instances (state, last_seen_at)",
        "CREATE INDEX ix_evidence_records_run_observed ON evidence_records (rca_run_id, observed_at)",
        "CREATE INDEX ix_incident_messages_incident_created ON incident_messages (incident_id, created_at)",
        "CREATE INDEX ix_incident_timeline_incident_occurred ON incident_timeline_events (incident_id, occurred_at)",
        "CREATE INDEX ix_audit_events_resource_occurred ON audit_events (resource_type, resource_id, occurred_at)",
    ):
        op.execute(statement)


def _replace_tables() -> None:
    for table_name in (
        "ingestion_dedup_keys",
        "alert_instances",
        "incident_alerts",
        "hypothesis_evidence",
    ):
        op.execute(f"DROP TABLE {table_name}")

    for table_name in CANONICAL_TABLES:
        op.execute(f"ALTER TABLE {table_name} RENAME TO {LEGACY_TABLES[table_name]}")
    for table_name in CANONICAL_TABLES:
        op.execute(f"ALTER TABLE {table_name}_new RENAME TO {table_name}")
    for table_name in (
        "ingestion_dedup_keys",
        "alert_instances",
        "incident_alerts",
        "hypothesis_evidence",
    ):
        op.execute(f"ALTER TABLE {table_name}_new RENAME TO {table_name}")


def upgrade() -> None:
    # Alembic runs this revision in one transaction.  Do not add an autocommit
    # block: every create/copy/assertion/rename must succeed or be rolled back.
    # Alembic's default version column is VARCHAR(32), while this revision's
    # stable identifier is 33 characters long.
    op.execute(
        "ALTER TABLE alembic_version_backend ALTER COLUMN version_num TYPE VARCHAR(64)"
    )
    _assert_no_duplicate_uuids()
    for statement in REPLACEMENT_DDL:
        op.execute(statement)
    for statement in DEPENDENT_DDL:
        op.execute(statement)
    for statement in COPY_STATEMENTS:
        op.execute(statement)
    _assert_copy_counts_and_uuids()
    _rename_legacy_indexes()
    _replace_tables()
    _rebuild_indexes()


def downgrade() -> None:
    raise RuntimeError(
        "0003 cannot provide a lossless downgrade after ordinary-table writes; "
        "restore from the retained legacy partitioned tables or a backup instead."
    )
