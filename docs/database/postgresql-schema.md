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

## Final UUID-only runtime schema

The six canonical runtime relations are ordinary PostgreSQL tables. Each has a
single-column `id UUID PRIMARY KEY`; all foreign keys below use UUID columns
only. The canonical catalog requirement is `relkind = 'r'` and
`relispartition = false`. No routing helper or composite foreign key is part of
the runtime interface. During Backend-0003 conversion, **historical rows are copied and validated before cutover**; row counts, UUID uniqueness, required
fields, foreign keys, and representative reads/writes are checked before the
canonical names switch.

```sql
CREATE TABLE webhook_deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    received_at TIMESTAMPTZ NOT NULL,
    source_id UUID NOT NULL REFERENCES grafana_sources(id),
    token_id TEXT,
    body_hash TEXT NOT NULL,
    raw_body BYTEA NOT NULL,
    raw_payload JSONB NOT NULL,
    status TEXT NOT NULL,
    processed_at TIMESTAMPTZ,
    error_message TEXT
);

CREATE TABLE alert_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    observed_at TIMESTAMPTZ NOT NULL,
    source_id UUID NOT NULL REFERENCES grafana_sources(id),
    delivery_id UUID NOT NULL REFERENCES webhook_deliveries(id),
    fingerprint TEXT NOT NULL,
    alert_state TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    validation_errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    labels JSONB NOT NULL DEFAULT '{}'::jsonb,
    annotations JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_payload JSONB NOT NULL
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
