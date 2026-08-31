# PostgreSQL 資料庫結構參考文件

本文件說明不可變四階段轉換完成後的發布資料庫結構。實際可執行的資料庫結構以
Alembic 檔案為準，而不是以下範例。Backend 遷移會寫入
`alembic_version_backend`；RCA Worker 遷移會寫入
`alembic_version_rca_worker`。兩條遷移流程共用同一個應用程式角色，
但各自保留獨立的版本表與資料表所有權邊界。每組交錯遷移都必須遵循
**Backend 遷移 → RCA Worker 遷移**的跨流程順序：先執行
Backend-0002，再執行 Worker-0002；接著執行 Backend-0003，最後執行 Worker-0003。

## 維護窗口與四個遷移關卡

這是正式的維運操作流程。執行關卡 1 前，必須先完成經核准的備份，並且
**停止 Backend 與 RCA Worker 的所有寫入**。在關卡 4 與 Worker-0003 的
後置條件系統目錄檢查通過以前，所有執行期服務都必須維持停止狀態。
每個關卡都必須明確執行；不得改用隱含的最新版本目標，也不得透過直接修改版本表
來略過遷移。

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

每次查詢兩個版本表後，都必須確認兩筆資料符合預期完成的關卡，才能繼續下一步。
全新資料庫必須執行全部四個關卡。若資料庫目前已位於
**Worker-0002 head**，應先驗證 Backend-0002、Worker-0002 的版本資料與來源
系統目錄，然後**只執行關卡 3 與關卡 4**。對於任何已發布的遷移，
都**不得執行 `stamp` 或重新執行**。

如果在執行期服務重新啟動前有任一關卡或系統目錄檢查失敗，必須持續停止寫入，
並以經核准的備份為基礎進行調查。保留的舊版父資料表仍會留在原處。一旦新版本開始
寫入資料，就**無法透過 Alembic `downgrade` 還原**到轉換前完全相同的資料庫狀態。
**不得執行 Alembic `downgrade`**；應停止流量並從**經核准的備份**還原，或依照
明確的復原計畫遷移已驗證的資料差異。

## Backend-0001 與 Backend-0002 歷史參考清單

以下發布資料庫結構已取代六個分割式執行期父資料表，但本文件仍刻意保留
Backend-0001 的歷史清單，讓不可變遷移的演進關係可以接受稽核。這些內容
**不是**重新建立分割表或恢復複合鍵介面的操作指示。
`alembic_version_backend` 用來記錄 Backend 遷移流程的版本。

### Backend-0001 已發布基準

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

下方最終執行期 DDL 代表原本六個分割式父資料表，並刻意移除其舊有的分割鍵欄位。
舊版 `evidence_records` 的指標欄位僅供歷史參考；正式資料表使用
`raw_result BYTEA`、`metadata JSONB` 與 `content_hash`。

### Backend-0002 正規化與識別欄位變更

Backend-0002 另外加入下列永久保存的正規化物件與欄位變更。
`folder_code` **不是** `projects.id`；它是由 `folder_scope_mappings`
對應的來源資料夾識別碼。

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

其餘不可變的 Backend-0001 維運索引仍屬於歷史清單的一部分。即使正式執行期
資料表已取代原本的父資料表，這些索引名稱與條件仍會保留，以供遷移稽核。

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

## 最終僅使用 UUID 的執行期資料庫結構

六個正式執行期關聯都是一般 PostgreSQL 資料表。每張表都使用單一欄位的
`id UUID PRIMARY KEY`，而且下方所有外鍵都只使用 UUID 欄位。正式系統目錄必須符合
`relkind = 'r'` 與 `relispartition = false`。執行期介面不包含任何路由輔助函式或
複合外鍵。Backend-0003 轉換期間，會在切換前**複製並驗證歷史資料列**；只有在確認
資料列數量、UUID 唯一性、必填欄位、外鍵，以及代表性的讀寫操作後，才會切換正式名稱。

Backend-0002 完成後的 Incident 狀態會保留 `identity_version`、`provider`、
`folder_code` 與 `alert_name`。範圍欄位允許 NULL，僅是因為上方明確列出的
Backend-0002 變更已移除原有的 `NOT NULL` 限制。

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

RCA 生命週期與分析資料仍由 Worker 擁有：`worker_jobs` 與 `worker_attempts`
保留其租約、執行嘗試與失敗欄位；`specialist_runs` 保留分析稽核欄位；
`rca_reports` 則將 `result_status` 儲存為 `COMPLETE`、`PARTIAL` 或 `FAILED`。
完整證據會持久保存為 `raw_result BYTEA`、物件格式的 `metadata JSONB`
及 `content_hash`，不會加入僅允許結構化資料的 AI 上下文。
`EvidenceReference` 仍是只包含 ID 的 UUID 參照。

關卡 4 完成後，請執行下列唯讀系統目錄檢查：

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

前六筆資料必須是一般的正式關聯；後六筆則是保留的舊版父資料表。

## 保留的舊版分割式父資料表

轉換流程會保留下列六個歷史父資料表名稱，但不會自動清除：

```text
webhook_deliveries__partitioned_legacy_0003
alert_events__partitioned_legacy_0003
evidence_records__partitioned_legacy_0003
incident_messages__partitioned_legacy_0003
incident_timeline_events__partitioned_legacy_0003
audit_events__partitioned_legacy_0003
```

這些資料表用於稽核、切換後驗證，以及後續經明確核准的清理決策。它們不是第二套
執行期資料庫結構，也不會由任何自動化流程刪除。

## 範圍與所有權

`teams`、`projects`、`environments` 與 `services` 用來定義範圍。Backend
擁有範圍／來源、傳遞、告警、事件、時間軸、outbox 與稽核資料庫結構；RCA Worker
則擁有執行紀錄、specialist 執行紀錄、證據、假設、報告、工作與執行嘗試。
遷移所有權以機器可讀的
`contracts/database/table-ownership.yaml` 契約為準。

Backend、Worker 與兩條遷移流程使用同一個受限的應用程式角色。該角色具備必要的
應用程式 DML 與遷移 DDL 權限，但不具備 superuser、角色管理、database owner
或不相關資料庫結構的權限。

## 本機開發

`.env.compose.example` 僅用於設定本機基礎設施的連接埠。預設 PostgreSQL 端點為
`postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent`，Pub/Sub 模擬器
端點為 `127.0.0.1:58085`。執行期與遷移設定仍分別存放於
`.env.backend-api`、`.env.rca-worker`、`.env.backend-migration`、
`.env.rca-worker-migration` 與 `.env.compose`，並提供已提交的 `.example` 範本。
作業系統環境變數的優先順序較高；如果明確指定的覆寫檔案不存在，系統會拒絕啟動。
