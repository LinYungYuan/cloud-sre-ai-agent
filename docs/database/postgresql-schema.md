# PostgreSQL schema reference

This reference describes the release schema after the immutable four-gate
conversion. Alembic files, not the examples below, are the executable schema
authority. Backend migrations write `alembic_version_backend`; RCA Worker
migrations write `alembic_version_rca_worker`. The two streams share an
application role but retain separate version tables and table-ownership
boundaries.

## 維護窗口與四個 migration gate

This is the authoritative operator procedure. Before gate 1, take an approved
backup and **停止 Backend 與 RCA Worker 的所有 writes**. Keep every runtime
stopped until gate 4 and the Worker-0003 的 postcondition catalog checks pass.
Every gate is explicit; do not substitute an implicit latest target or use a
version-table shortcut.

```bash
(cd backend && BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade 0002_grafana_normalization_v2)
```

```sql
SELECT version_num FROM alembic_version_backend;
SELECT version_num FROM alembic_version_rca_worker;
```

```bash
(cd rca-worker && RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade 0002_adk_specialist_analysis)
```

```sql
SELECT version_num FROM alembic_version_backend;
SELECT version_num FROM alembic_version_rca_worker;
```

```bash
(cd backend && BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade 0003_non_partition_runtime_tables)
```

```sql
SELECT version_num FROM alembic_version_backend;
SELECT version_num FROM alembic_version_rca_worker;
```

```bash
(cd rca-worker && RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade 0003_validate_ordinary_runtime_tables)
```

```sql
SELECT version_num FROM alembic_version_backend;
SELECT version_num FROM alembic_version_rca_worker;
```

After each query pair, compare both rows with the expected completed gate
before continuing. A clean database runs all four gates. An existing
**既有 Worker-0002 head** database first verifies the Backend-0002 and
Worker-0002 version rows and their source catalog, then **只執行 gate 3 與 gate 4**.
It must **不得 stamp 或 replay** either published migration.

If a gate or catalog check fails before runtime restart, leave writes stopped
and investigate from the approved backup. Retained legacy parents remain in
place. After new writes, **不得執行 Alembic downgrade**: stop traffic and
restore from an **approved backup** or migrate the validated delta under an
explicit recovery plan.

## Historical Backend-0001 and Backend-0002 reference manifest

The release schema below supersedes the six partitioned runtime parents, but
this reference intentionally retains the historical Backend-0001 inventory.
It makes the immutable migration ancestry auditable; it is **not** an
instruction to recreate partitioned tables or to restore their composite-key
interface. `alembic_version_backend` records the Backend stream version.

### Backend-0001 published baseline

```sql
CREATE TABLE teams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), team_id UUID NOT NULL REFERENCES teams(id),
    name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (team_id, name), UNIQUE (team_id, id)
);
CREATE TABLE environments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), project_id UUID NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name), UNIQUE (project_id, id)
);
CREATE TABLE services (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), environment_id UUID NOT NULL REFERENCES environments(id),
    name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (environment_id, name), UNIQUE (environment_id, id)
);
CREATE TABLE subjects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), external_id TEXT NOT NULL UNIQUE,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('USER', 'SERVICE_ACCOUNT')),
    display_name TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE scope_grants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), subject_id UUID NOT NULL REFERENCES subjects(id),
    team_id UUID REFERENCES teams(id), project_id UUID REFERENCES projects(id),
    environment_id UUID REFERENCES environments(id), service_id UUID REFERENCES services(id),
    role TEXT NOT NULL CHECK (role IN ('VIEWER', 'OPERATOR', 'ADMIN')), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(team_id, project_id, environment_id, service_id) = 1)
);
CREATE TABLE grafana_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), project_id UUID NOT NULL REFERENCES projects(id),
    environment_id UUID NOT NULL REFERENCES environments(id), name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, environment_id, name), FOREIGN KEY (project_id, environment_id) REFERENCES environments(project_id, id)
);
CREATE TABLE ingestion_dedup_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), source_id UUID NOT NULL REFERENCES grafana_sources(id),
    dedup_key TEXT NOT NULL, delivery_id UUID NOT NULL, delivery_partition_timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (delivery_id, delivery_partition_timestamp) REFERENCES webhook_deliveries(id, partition_timestamp),
    UNIQUE (source_id, dedup_key)
);
CREATE TABLE alert_instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), source_id UUID NOT NULL REFERENCES grafana_sources(id),
    fingerprint TEXT NOT NULL, latest_event_id UUID NOT NULL, latest_event_partition_timestamp TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('FIRING', 'RESOLVED')), labels JSONB NOT NULL DEFAULT '{}'::jsonb,
    annotations JSONB NOT NULL DEFAULT '{}'::jsonb, first_seen_at TIMESTAMPTZ NOT NULL, last_seen_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ, version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    FOREIGN KEY (latest_event_id, latest_event_partition_timestamp) REFERENCES alert_events(id, partition_timestamp),
    UNIQUE (source_id, fingerprint)
);
CREATE TABLE classification_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), source_id UUID REFERENCES grafana_sources(id), matcher JSONB NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0, team_id UUID REFERENCES teams(id), project_id UUID REFERENCES projects(id),
    environment_id UUID REFERENCES environments(id), service_id UUID REFERENCES services(id), enabled BOOLEAN NOT NULL DEFAULT true,
    created_by UUID REFERENCES subjects(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE incidents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), incident_number BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    identity_key TEXT NOT NULL, title TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL,
    alert_state TEXT NOT NULL, team_id UUID NOT NULL REFERENCES teams(id), project_id UUID NOT NULL REFERENCES projects(id),
    environment_id UUID NOT NULL REFERENCES environments(id), service_id UUID REFERENCES services(id),
    acknowledged_at TIMESTAMPTZ, acknowledged_by UUID REFERENCES subjects(id), assigned_to UUID REFERENCES subjects(id),
    opened_at TIMESTAMPTZ NOT NULL, resolved_at TIMESTAMPTZ, reopened_from_incident_id UUID REFERENCES incidents(id),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0), created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE incident_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), incident_id UUID NOT NULL REFERENCES incidents(id),
    alert_event_id UUID NOT NULL, alert_event_partition_timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (alert_event_id, alert_event_partition_timestamp) REFERENCES alert_events(id, partition_timestamp),
    UNIQUE (incident_id, alert_event_id, alert_event_partition_timestamp)
);
CREATE TABLE incident_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), incident_id UUID NOT NULL REFERENCES incidents(id),
    assignee_id UUID NOT NULL REFERENCES subjects(id), assigned_by UUID REFERENCES subjects(id),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(), unassigned_at TIMESTAMPTZ
);
CREATE TABLE incident_status_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), incident_id UUID NOT NULL REFERENCES incidents(id),
    from_status TEXT, to_status TEXT NOT NULL, changed_by UUID REFERENCES subjects(id),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(), reason TEXT
);
CREATE TABLE rca_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), incident_id UUID NOT NULL REFERENCES incidents(id), status TEXT NOT NULL,
    started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), error_message TEXT
);
CREATE TABLE specialist_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), rca_run_id UUID NOT NULL REFERENCES rca_runs(id), specialist_type TEXT NOT NULL,
    status TEXT NOT NULL, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_message TEXT, UNIQUE (rca_run_id, specialist_type)
);
CREATE TABLE rca_hypotheses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), rca_run_id UUID NOT NULL REFERENCES rca_runs(id),
    statement TEXT NOT NULL, confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE hypothesis_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), hypothesis_id UUID NOT NULL REFERENCES rca_hypotheses(id),
    evidence_id UUID NOT NULL, evidence_partition_timestamp TIMESTAMPTZ NOT NULL, relation TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (evidence_id, evidence_partition_timestamp) REFERENCES evidence_records(id, partition_timestamp),
    UNIQUE (hypothesis_id, evidence_id, evidence_partition_timestamp, relation)
);
CREATE TABLE rca_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), rca_run_id UUID NOT NULL REFERENCES rca_runs(id),
    version INTEGER NOT NULL CHECK (version > 0), summary TEXT NOT NULL, report JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (rca_run_id, version)
);
CREATE TABLE outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), aggregate_type TEXT NOT NULL, aggregate_id UUID NOT NULL,
    event_type TEXT NOT NULL, payload JSONB NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'PENDING', available_at TIMESTAMPTZ NOT NULL DEFAULT now(), published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE worker_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), rca_run_id UUID NOT NULL REFERENCES rca_runs(id), job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED', payload JSONB NOT NULL, available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE worker_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), worker_job_id UUID NOT NULL REFERENCES worker_jobs(id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0), started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ, error_message TEXT, UNIQUE (worker_job_id, attempt_number)
);
```

The six partitioned parents are represented by the final runtime DDL below,
which intentionally replaces their old partition-key columns. The old
`evidence_records` pointer column is historical only; the canonical table has
`raw_result BYTEA`, `metadata JSONB`, and `content_hash`.

### Backend-0002 normalization and identity mutations

Backend-0002 additionally introduced the following persistent normalization
objects and altered columns. `folder_code is not projects.id`; it is a source
folder identifier mapped by `folder_scope_mappings`.

```sql
CREATE TABLE normalization_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), source_id UUID NULL REFERENCES grafana_sources(id),
    name TEXT NOT NULL, version INTEGER NOT NULL, priority INTEGER NOT NULL, provider TEXT NOT NULL,
    conditions JSONB NOT NULL, output JSONB NOT NULL, enabled BOOLEAN NOT NULL DEFAULT true,
    created_by UUID NULL REFERENCES subjects(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_normalization_rules_source_name_version UNIQUE NULLS NOT DISTINCT (source_id, name, version)
);
CREATE TABLE folder_scope_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), source_id UUID NOT NULL REFERENCES grafana_sources(id),
    folder_code TEXT NOT NULL, team_id UUID NULL REFERENCES teams(id), project_id UUID NULL REFERENCES projects(id),
    environment_id UUID NULL REFERENCES environments(id), service_id UUID NULL REFERENCES services(id),
    enabled BOOLEAN NOT NULL DEFAULT true, created_by UUID NULL REFERENCES subjects(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_folder_scope_source_folder UNIQUE (source_id, folder_code)
);
ALTER TABLE webhook_deliveries ADD COLUMN truncated_alerts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE webhook_deliveries ADD COLUMN incomplete BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE alert_events ADD COLUMN provider TEXT NULL;
ALTER TABLE incidents ADD COLUMN identity_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE incidents ADD COLUMN provider TEXT NULL;
ALTER TABLE incidents ADD COLUMN folder_code TEXT NULL;
ALTER TABLE incidents ADD COLUMN alert_name TEXT NULL;
ALTER TABLE incidents ALTER COLUMN team_id DROP NOT NULL;
ALTER TABLE incidents ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE incidents ALTER COLUMN environment_id DROP NOT NULL;
CREATE UNIQUE INDEX uq_incidents_active_identity
    ON incidents (identity_version, identity_key)
    WHERE status IN ('OPEN', 'INVESTIGATING');
CREATE INDEX ix_normalization_rules_lookup ON normalization_rules (source_id, enabled, priority);
CREATE INDEX ix_folder_scope_mappings_lookup ON folder_scope_mappings (source_id, enabled, folder_code);
```

The remaining immutable Backend-0001 operational indexes remain part of the
historical manifest. Their names and predicates are retained for migration
audit even where a canonical runtime table has since replaced a parent.

```sql
CREATE UNIQUE INDEX uq_rca_runs_active_incident
    ON rca_runs (incident_id)
    WHERE status IN ('WAITING_FOR_CLASSIFICATION', 'QUEUED', 'RUNNING');
CREATE UNIQUE INDEX uq_worker_jobs_run_type ON worker_jobs (rca_run_id, job_type);
CREATE INDEX ix_webhook_deliveries_source_received ON webhook_deliveries (source_id, received_at);
CREATE INDEX ix_alert_events_source_fingerprint_observed ON alert_events (source_id, fingerprint, observed_at);
CREATE INDEX ix_alert_instances_state_last_seen ON alert_instances (state, last_seen_at);
CREATE INDEX ix_incidents_scope_status_opened ON incidents (project_id, environment_id, status, opened_at);
CREATE INDEX ix_evidence_records_run_observed ON evidence_records (rca_run_id, observed_at);
CREATE INDEX ix_incident_messages_incident_created ON incident_messages (incident_id, created_at);
CREATE INDEX ix_incident_timeline_incident_occurred ON incident_timeline_events (incident_id, occurred_at);
CREATE INDEX ix_audit_events_resource_occurred ON audit_events (resource_type, resource_id, occurred_at);
CREATE INDEX ix_worker_jobs_status_available ON worker_jobs (status, available_at);
CREATE INDEX ix_outbox_events_status_available ON outbox_events (status, available_at);
```

## Final UUID-only runtime schema

The six canonical runtime relations are ordinary PostgreSQL tables. Each has a
single-column `id UUID PRIMARY KEY`; all foreign keys below use UUID columns
only. The canonical catalog requirement is `relkind = 'r'` and
`relispartition = false`. No routing helper or composite foreign key is part of
the runtime interface. During Backend-0003 conversion, **historical rows are copied and validated before cutover**; row counts, UUID uniqueness, required
fields, foreign keys, and representative reads/writes are checked before the
canonical names switch.

The post-Backend-0002 Incident state retains `identity_version`, `provider`,
`folder_code`, and `alert_name`; scope columns are nullable only because the
explicit Backend-0002 mutations above dropped their original `NOT NULL`
requirements.

```sql
CREATE TABLE webhook_deliveries (
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
);

CREATE TABLE alert_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    observed_at TIMESTAMPTZ NOT NULL,
    source_id UUID NOT NULL REFERENCES grafana_sources(id),
    delivery_id UUID NOT NULL REFERENCES webhook_deliveries(id),
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
);

CREATE TABLE evidence_records (
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
    raw_result BYTEA NOT NULL,
    metadata JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    CHECK (time_window_end >= time_window_start),
    CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE TABLE incident_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    incident_id UUID NOT NULL REFERENCES incidents(id),
    role TEXT NOT NULL,
    subject_id UUID REFERENCES subjects(id),
    rca_run_id UUID REFERENCES rca_runs(id),
    content TEXT NOT NULL,
    evidence_references JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE incident_timeline_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at TIMESTAMPTZ NOT NULL,
    incident_id UUID NOT NULL REFERENCES incidents(id),
    event_type TEXT NOT NULL,
    actor_id UUID REFERENCES subjects(id),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id UUID REFERENCES subjects(id),
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id UUID,
    scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    before_value JSONB,
    after_value JSONB
);
```

RCA lifecycle and analysis remain Worker-owned: `worker_jobs` and
`worker_attempts` preserve their lease, attempt, and failure fields;
`specialist_runs` preserves its analysis audit fields; and `rca_reports` stores
`result_status` as `COMPLETE`, `PARTIAL`, or `FAILED`. Exact evidence remains
durable as `raw_result BYTEA`, object-shaped `metadata JSONB`, and
`content_hash`; it is not added to the structured-data-only AI context.
`EvidenceReference` remains an id-only UUID reference.

Use the following read-only catalog check after gate 4:

```sql
SELECT c.relname, c.relkind, c.relispartition
FROM pg_class AS c
WHERE c.relname IN (
  'webhook_deliveries', 'alert_events', 'evidence_records',
  'incident_messages', 'incident_timeline_events', 'audit_events',
  'webhook_deliveries__partitioned_legacy_0003',
  'alert_events__partitioned_legacy_0003',
  'evidence_records__partitioned_legacy_0003',
  'incident_messages__partitioned_legacy_0003',
  'incident_timeline_events__partitioned_legacy_0003',
  'audit_events__partitioned_legacy_0003'
) ORDER BY c.relname;
```

The first six rows must be ordinary canonical relations; the final six are the
retained legacy parents.

## Retained legacy partition parents

The conversion preserves, but does not automatically clean up, exactly these
six historical parent names:

```text
webhook_deliveries__partitioned_legacy_0003
alert_events__partitioned_legacy_0003
evidence_records__partitioned_legacy_0003
incident_messages__partitioned_legacy_0003
incident_timeline_events__partitioned_legacy_0003
audit_events__partitioned_legacy_0003
```

They exist for audit, post-cutover validation, and an explicit later cleanup
decision. They are not a second runtime schema and no automatic process drops
them.

## Scope and ownership

`teams`, `projects`, `environments`, and `services` define scope. Backend owns
scope/source, delivery, alert, incident, timeline, outbox, and audit schema;
the RCA Worker owns runs, specialist runs, evidence, hypotheses, reports, jobs,
and attempts. The machine-readable ownership contract in
`contracts/database/table-ownership.yaml` is authoritative for migration
ownership.

Backend, Worker, and both migration streams use the same restricted
application role. It has the needed application DML and migration DDL but no
superuser, role-management, database-owner, or unrelated-schema privileges.

## Local development

Use `.env.compose.example` only for local infrastructure ports. The default
PostgreSQL endpoint is `postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent`
and the Pub/Sub emulator endpoint is `127.0.0.1:58085`. Runtime and migration
configuration remain isolated in `.env.backend-api`, `.env.rca-worker`,
`.env.backend-migration`, `.env.rca-worker-migration`, and `.env.compose` (with
their committed `.example` templates). OS environment values take precedence;
a named missing override file fails closed.
