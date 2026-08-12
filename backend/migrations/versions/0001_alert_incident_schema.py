"""Create the alert ingestion, incident, RCA, and audit schema."""

from collections.abc import Sequence
from datetime import UTC, datetime

from alembic import op

revision: str = "0001_alert_incident_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PARTITIONED_TABLES = (
    "webhook_deliveries",
    "alert_events",
    "evidence_records",
    "incident_messages",
    "incident_timeline_events",
    "audit_events",
)


DDL = (
    """CREATE TABLE teams (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE projects (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        team_id UUID NOT NULL REFERENCES teams(id),
        name TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (team_id, name)
    )""",
    """CREATE TABLE environments (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id UUID NOT NULL REFERENCES projects(id),
        name TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (project_id, name)
    )""",
    """CREATE TABLE services (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        environment_id UUID NOT NULL REFERENCES environments(id),
        name TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (environment_id, name)
    )""",
    """CREATE TABLE subjects (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        external_id TEXT NOT NULL UNIQUE,
        subject_type TEXT NOT NULL CHECK (subject_type IN ('USER', 'SERVICE_ACCOUNT')),
        display_name TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE scope_grants (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        subject_id UUID NOT NULL REFERENCES subjects(id),
        team_id UUID REFERENCES teams(id),
        project_id UUID REFERENCES projects(id),
        environment_id UUID REFERENCES environments(id),
        service_id UUID REFERENCES services(id),
        role TEXT NOT NULL CHECK (role IN ('VIEWER', 'OPERATOR', 'ADMIN')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK (num_nonnulls(team_id, project_id, environment_id, service_id) = 1)
    )""",
    """CREATE TABLE grafana_sources (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id UUID NOT NULL REFERENCES projects(id),
        environment_id UUID NOT NULL REFERENCES environments(id),
        name TEXT NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (project_id, environment_id, name)
    )""",
    """CREATE TABLE webhook_deliveries (
        id UUID NOT NULL DEFAULT gen_random_uuid(),
        partition_timestamp TIMESTAMPTZ NOT NULL,
        received_at TIMESTAMPTZ NOT NULL,
        source_id UUID NOT NULL REFERENCES grafana_sources(id),
        token_id TEXT,
        body_hash TEXT NOT NULL,
        raw_payload JSONB NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('RECEIVED', 'PROCESSED', 'DUPLICATE', 'VALIDATION_FAILED', 'REJECTED', 'FAILED')),
        processed_at TIMESTAMPTZ,
        error_message TEXT,
        PRIMARY KEY (id, partition_timestamp)
    ) PARTITION BY RANGE (partition_timestamp)""",
    """CREATE TABLE ingestion_dedup_keys (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        source_id UUID NOT NULL REFERENCES grafana_sources(id),
        dedup_key TEXT NOT NULL,
        delivery_id UUID NOT NULL,
        delivery_partition_timestamp TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        FOREIGN KEY (delivery_id, delivery_partition_timestamp)
            REFERENCES webhook_deliveries(id, partition_timestamp),
        UNIQUE (source_id, dedup_key)
    )""",
    """CREATE TABLE alert_events (
        id UUID NOT NULL DEFAULT gen_random_uuid(),
        partition_timestamp TIMESTAMPTZ NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        source_id UUID NOT NULL REFERENCES grafana_sources(id),
        delivery_id UUID NOT NULL,
        delivery_partition_timestamp TIMESTAMPTZ NOT NULL,
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
        PRIMARY KEY (id, partition_timestamp),
        FOREIGN KEY (delivery_id, delivery_partition_timestamp)
            REFERENCES webhook_deliveries(id, partition_timestamp)
    ) PARTITION BY RANGE (partition_timestamp)""",
    """CREATE TABLE alert_instances (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        source_id UUID NOT NULL REFERENCES grafana_sources(id),
        fingerprint TEXT NOT NULL,
        latest_event_id UUID NOT NULL,
        latest_event_partition_timestamp TIMESTAMPTZ NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('FIRING', 'RESOLVED')),
        labels JSONB NOT NULL DEFAULT '{}'::jsonb,
        annotations JSONB NOT NULL DEFAULT '{}'::jsonb,
        first_seen_at TIMESTAMPTZ NOT NULL,
        last_seen_at TIMESTAMPTZ NOT NULL,
        resolved_at TIMESTAMPTZ,
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        FOREIGN KEY (latest_event_id, latest_event_partition_timestamp)
            REFERENCES alert_events(id, partition_timestamp),
        UNIQUE (source_id, fingerprint)
    )""",
    """CREATE TABLE classification_mappings (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        source_id UUID REFERENCES grafana_sources(id),
        matcher JSONB NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        team_id UUID REFERENCES teams(id),
        project_id UUID REFERENCES projects(id),
        environment_id UUID REFERENCES environments(id),
        service_id UUID REFERENCES services(id),
        enabled BOOLEAN NOT NULL DEFAULT true,
        created_by UUID REFERENCES subjects(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK (num_nonnulls(team_id, project_id, environment_id, service_id) >= 1)
    )""",
    """CREATE TABLE incidents (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        incident_number BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
        identity_key TEXT NOT NULL,
        title TEXT NOT NULL,
        severity TEXT NOT NULL CHECK (severity IN ('SEV1', 'SEV2', 'SEV3', 'SEV4')),
        status TEXT NOT NULL CHECK (status IN ('OPEN', 'INVESTIGATING', 'RESOLVED')),
        alert_state TEXT NOT NULL CHECK (alert_state IN ('FIRING', 'RESOLVED')),
        team_id UUID NOT NULL REFERENCES teams(id),
        project_id UUID NOT NULL REFERENCES projects(id),
        environment_id UUID NOT NULL REFERENCES environments(id),
        service_id UUID REFERENCES services(id),
        acknowledged_at TIMESTAMPTZ,
        acknowledged_by UUID REFERENCES subjects(id),
        assigned_to UUID REFERENCES subjects(id),
        opened_at TIMESTAMPTZ NOT NULL,
        resolved_at TIMESTAMPTZ,
        reopened_from_incident_id UUID REFERENCES incidents(id),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE incident_alerts (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        incident_id UUID NOT NULL REFERENCES incidents(id),
        alert_event_id UUID NOT NULL,
        alert_event_partition_timestamp TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        FOREIGN KEY (alert_event_id, alert_event_partition_timestamp)
            REFERENCES alert_events(id, partition_timestamp),
        UNIQUE (incident_id, alert_event_id, alert_event_partition_timestamp)
    )""",
    """CREATE TABLE incident_assignments (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        incident_id UUID NOT NULL REFERENCES incidents(id),
        assignee_id UUID NOT NULL REFERENCES subjects(id),
        assigned_by UUID REFERENCES subjects(id),
        assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        unassigned_at TIMESTAMPTZ
    )""",
    """CREATE TABLE incident_status_history (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        incident_id UUID NOT NULL REFERENCES incidents(id),
        from_status TEXT CHECK (from_status IN ('OPEN', 'INVESTIGATING', 'RESOLVED')),
        to_status TEXT NOT NULL CHECK (to_status IN ('OPEN', 'INVESTIGATING', 'RESOLVED')),
        changed_by UUID REFERENCES subjects(id),
        changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        reason TEXT
    )""",
    """CREATE TABLE rca_runs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        incident_id UUID NOT NULL REFERENCES incidents(id),
        status TEXT NOT NULL CHECK (status IN (
            'WAITING_FOR_CLASSIFICATION', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED'
        )),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        error_message TEXT
    )""",
    """CREATE TABLE specialist_runs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        rca_run_id UUID NOT NULL REFERENCES rca_runs(id),
        specialist_type TEXT NOT NULL CHECK (specialist_type IN ('METRICS', 'TRACES', 'LOGS')),
        status TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED')),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        error_message TEXT,
        UNIQUE (rca_run_id, specialist_type)
    )""",
    """CREATE TABLE evidence_records (
        id UUID NOT NULL DEFAULT gen_random_uuid(),
        partition_timestamp TIMESTAMPTZ NOT NULL,
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
        PRIMARY KEY (id, partition_timestamp),
        CHECK (time_window_end >= time_window_start)
    ) PARTITION BY RANGE (partition_timestamp)""",
    """CREATE TABLE rca_hypotheses (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        rca_run_id UUID NOT NULL REFERENCES rca_runs(id),
        statement TEXT NOT NULL,
        confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE hypothesis_evidence (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        hypothesis_id UUID NOT NULL REFERENCES rca_hypotheses(id),
        evidence_id UUID NOT NULL,
        evidence_partition_timestamp TIMESTAMPTZ NOT NULL,
        relation TEXT NOT NULL CHECK (relation IN ('SUPPORTS', 'CONTRADICTS', 'MISSING')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        FOREIGN KEY (evidence_id, evidence_partition_timestamp)
            REFERENCES evidence_records(id, partition_timestamp),
        UNIQUE (hypothesis_id, evidence_id, evidence_partition_timestamp, relation)
    )""",
    """CREATE TABLE rca_reports (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        rca_run_id UUID NOT NULL REFERENCES rca_runs(id),
        version INTEGER NOT NULL CHECK (version > 0),
        summary TEXT NOT NULL,
        report JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (rca_run_id, version)
    )""",
    """CREATE TABLE incident_messages (
        id UUID NOT NULL DEFAULT gen_random_uuid(),
        partition_timestamp TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        incident_id UUID NOT NULL REFERENCES incidents(id),
        role TEXT NOT NULL CHECK (role IN ('USER', 'AGENT', 'SYSTEM')),
        subject_id UUID REFERENCES subjects(id),
        rca_run_id UUID REFERENCES rca_runs(id),
        content TEXT NOT NULL,
        evidence_references JSONB NOT NULL DEFAULT '[]'::jsonb,
        PRIMARY KEY (id, partition_timestamp)
    ) PARTITION BY RANGE (partition_timestamp)""",
    """CREATE TABLE incident_timeline_events (
        id UUID NOT NULL DEFAULT gen_random_uuid(),
        partition_timestamp TIMESTAMPTZ NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        incident_id UUID NOT NULL REFERENCES incidents(id),
        event_type TEXT NOT NULL,
        actor_id UUID REFERENCES subjects(id),
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        PRIMARY KEY (id, partition_timestamp)
    ) PARTITION BY RANGE (partition_timestamp)""",
    """CREATE TABLE audit_events (
        id UUID NOT NULL DEFAULT gen_random_uuid(),
        partition_timestamp TIMESTAMPTZ NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        actor_id UUID REFERENCES subjects(id),
        action TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        resource_id UUID,
        scope JSONB NOT NULL DEFAULT '{}'::jsonb,
        before_value JSONB,
        after_value JSONB,
        PRIMARY KEY (id, partition_timestamp)
    ) PARTITION BY RANGE (partition_timestamp)""",
    """CREATE TABLE outbox_events (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        aggregate_type TEXT NOT NULL,
        aggregate_id UUID NOT NULL,
        event_type TEXT NOT NULL,
        payload JSONB NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'PUBLISHED', 'FAILED')),
        available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        published_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE worker_jobs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        rca_run_id UUID NOT NULL REFERENCES rca_runs(id),
        job_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
        payload JSONB NOT NULL,
        available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE worker_attempts (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        worker_job_id UUID NOT NULL REFERENCES worker_jobs(id),
        attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ,
        error_message TEXT,
        UNIQUE (worker_job_id, attempt_number)
    )""",
    """CREATE UNIQUE INDEX uq_rca_runs_active_incident
        ON rca_runs (incident_id)
        WHERE status IN ('WAITING_FOR_CLASSIFICATION', 'QUEUED', 'RUNNING')""",
    """CREATE UNIQUE INDEX uq_incidents_active_identity
        ON incidents (identity_key)
        WHERE status IN ('OPEN', 'INVESTIGATING')""",
    "CREATE UNIQUE INDEX uq_worker_jobs_run_type ON worker_jobs (rca_run_id, job_type)",
    "CREATE INDEX ix_webhook_deliveries_source_received ON webhook_deliveries (source_id, received_at)",
    "CREATE INDEX ix_alert_events_source_fingerprint_observed ON alert_events (source_id, fingerprint, observed_at)",
    "CREATE INDEX ix_alert_instances_state_last_seen ON alert_instances (state, last_seen_at)",
    "CREATE INDEX ix_incidents_scope_status_opened ON incidents (project_id, environment_id, status, opened_at)",
    "CREATE INDEX ix_evidence_records_run_observed ON evidence_records (rca_run_id, observed_at)",
    "CREATE INDEX ix_incident_messages_incident_created ON incident_messages (incident_id, created_at)",
    "CREATE INDEX ix_incident_timeline_incident_occurred ON incident_timeline_events (incident_id, occurred_at)",
    "CREATE INDEX ix_audit_events_resource_occurred ON audit_events (resource_type, resource_id, occurred_at)",
    "CREATE INDEX ix_worker_jobs_status_available ON worker_jobs (status, available_at)",
    "CREATE INDEX ix_outbox_events_status_available ON outbox_events (status, available_at)",
)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _create_partitions(year: int, month: int) -> None:
    next_year, next_month = _next_month(year, month)
    start = f"{year:04d}-{month:02d}-01 00:00:00+00"
    end = f"{next_year:04d}-{next_month:02d}-01 00:00:00+00"
    for table_name in PARTITIONED_TABLES:
        op.execute(
            f"CREATE TABLE {table_name}_{year:04d}_{month:02d} "
            f"PARTITION OF {table_name} FOR VALUES FROM ('{start}') TO ('{end}')"
        )


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)

    now = datetime.now(UTC)
    _create_partitions(now.year, now.month)
    next_year, next_month = _next_month(now.year, now.month)
    _create_partitions(next_year, next_month)


def downgrade() -> None:
    for table_name in (
        "worker_attempts",
        "worker_jobs",
        "outbox_events",
        "audit_events",
        "incident_timeline_events",
        "incident_messages",
        "rca_reports",
        "hypothesis_evidence",
        "rca_hypotheses",
        "evidence_records",
        "specialist_runs",
        "rca_runs",
        "incident_status_history",
        "incident_assignments",
        "incident_alerts",
        "incidents",
        "classification_mappings",
        "alert_instances",
        "alert_events",
        "ingestion_dedup_keys",
        "webhook_deliveries",
        "grafana_sources",
        "scope_grants",
        "subjects",
        "services",
        "environments",
        "projects",
        "teams",
    ):
        op.execute(f"DROP TABLE {table_name}")
