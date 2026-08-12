# PostgreSQL Schema 參考

## 適用範圍與權威來源

本文件是目前 SRE Agent 告警接收、Incident、RCA 與稽核資料模型的**閱讀與審查參考**，適用於 PostgreSQL 18。唯一可執行、可演進的 schema 來源是 Alembic revision `0001_alert_incident_schema`；本文 SQL 不可直接當作 migration 執行，也不取代 `alembic upgrade` 或 `alembic downgrade`。修改 migration 的資料表、約束、索引或分割邏輯時，必須在同一變更同步更新本文件。

## 本機啟動與 migration

在 repository 根目錄以本機開發設定啟動 PostgreSQL 18：

```sh
docker compose up -d postgres
docker compose exec postgres pg_isready -U postgres -d sre_agent
cd backend
UV_CACHE_DIR=.uv-cache uv run alembic upgrade head
```

`docker-compose.yml` 的本機範例帳密是資料庫 `sre_agent`、使用者 `postgres`、密碼 `postgres`，並透過本機連接埠 `55432` 使用；它們不是正式環境憑證。若要在本機重建至 migration 前狀態，執行下列指令。**警告：`alembic downgrade base` 會刪除本 schema 的資料表與其中所有資料，只能用於可丟棄的本機資料庫。**

```sh
cd backend
UV_CACHE_DIR=.uv-cache uv run alembic downgrade base
UV_CACHE_DIR=.uv-cache uv run alembic upgrade head
```

## 共同規則

- 識別碼以 `UUID` 表示，預設由 `gen_random_uuid()` 生成。
- 所有事件與生命週期時間使用 UTC 的 `TIMESTAMPTZ`；不要以無時區 timestamp 解讀資料。
- 半結構化欄位使用 `JSONB`。`webhook_deliveries.raw_payload` 與 `alert_events.raw_payload` 是已接受原始 payload 的永久保留紀錄，不應覆寫或刪除以取代處理後資料。
- 任一 alert 驗證失敗時，已接受的 delivery 仍會保留並標記 `status = 'VALIDATION_FAILED'`；同一 webhook 內其他有效 alerts 仍可繼續處理。每筆已取得 dedup claim 的失敗 alert event 也會保留，使用 `validation_status = 'VALIDATION_FAILED'`，並將結構化失敗原因寫入 `validation_errors` JSONB array；原始 payload 保持不變，以便稽核、重播與修正 parser 後重新處理。通過驗證的 event 使用預設值 `VALID` 與空 array。
- 六張分割母表以 `(id, partition_timestamp)` 為複合主鍵。參照分割表的外鍵必須同時保存識別碼與 `partition_timestamp`，才能定位到正確資料分區。

## 關係總覽

```mermaid
flowchart LR
    Team[teams] --> Project[projects] --> Environment[environments] --> Service[services]
    Subject[subjects] --> Grant[scope_grants]
    Project --> Source[grafana_sources]
    Environment --> Source
    Source --> Delivery[webhook_deliveries]
    Delivery --> Event[alert_events] --> Instance[alert_instances]
    Event --> IncidentAlert[incident_alerts] --> Incident[incidents]
    Incident --> RCA[rca_runs] --> Specialist[specialist_runs] --> Evidence[evidence_records]
    RCA --> Report[rca_reports]
    Incident --> Message[incident_messages]
    Incident --> Timeline[incident_timeline_events]
    RCA --> Job[worker_jobs] --> Attempt[worker_attempts]
    Incident -. "邏輯工作流／通用 aggregate" .-> Outbox[outbox_events]
    Subject --> Audit[audit_events]
```

Scope 的階層為 team、project、environment、service；`scope_grants` 授予 subject 單一層級的存取範圍。Grafana delivery 會產生 alert event，event 透過 `incident_alerts` 關聯到 Incident；Incident 再承載指派、狀態歷程、RCA、訊息與時間線。RCA 的專家執行會產生 evidence、hypothesis 與報告；背景工作與 outbox 分別處理非同步工作與事件發布。Incident 到 `outbox_events` 的虛線表示邏輯工作流：outbox 使用通用 `aggregate_type`／`aggregate_id`，並沒有指向 `incidents` 的外鍵。

## 非分割資料表

### Scope、身分與 Grafana 來源

`teams`、`projects`、`environments` 與 `services` 構成 scope 階層；`subjects` 是使用者或服務帳號，`scope_grants` 限定每列授權恰好對應一個 scope 層級。`grafana_sources` 屬於 project 與 environment，是 webhook 的來源。

```sql
CREATE TABLE teams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)

CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams(id),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (team_id, name)
)

CREATE TABLE environments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
)

CREATE TABLE services (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id UUID NOT NULL REFERENCES environments(id),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (environment_id, name)
)

CREATE TABLE subjects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id TEXT NOT NULL UNIQUE,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('USER', 'SERVICE_ACCOUNT')),
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)

CREATE TABLE scope_grants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_id UUID NOT NULL REFERENCES subjects(id),
    team_id UUID REFERENCES teams(id),
    project_id UUID REFERENCES projects(id),
    environment_id UUID REFERENCES environments(id),
    service_id UUID REFERENCES services(id),
    role TEXT NOT NULL CHECK (role IN ('VIEWER', 'OPERATOR', 'ADMIN')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(team_id, project_id, environment_id, service_id) = 1)
)

CREATE TABLE grafana_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id),
    environment_id UUID NOT NULL REFERENCES environments(id),
    name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, environment_id, name)
)
```

### 接收去重、Alert 與分類

`ingestion_dedup_keys` 以來源與 dedup key 避免重複接收；它以複合外鍵連回已分割的 delivery。`alert_instances` 保存同一 fingerprint 的最新狀態，`classification_mappings` 以 JSONB matcher 對應至一個或多個 scope。

```sql
CREATE TABLE ingestion_dedup_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES grafana_sources(id),
    dedup_key TEXT NOT NULL,
    delivery_id UUID NOT NULL,
    delivery_partition_timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (delivery_id, delivery_partition_timestamp)
        REFERENCES webhook_deliveries(id, partition_timestamp),
    UNIQUE (source_id, dedup_key)
)

CREATE TABLE alert_instances (
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
)

CREATE TABLE classification_mappings (
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
)
```

### Incident 與 RCA

`incidents` 是處理中的核心記錄，`identity_key` 保存 canonical incident identity 的 SHA-256；資料庫只允許每個 identity 同時有一筆 `OPEN` 或 `INVESTIGATING` Incident，讓併發 ingest 由 unique index 仲裁，`RESOLVED` 後則可依相同 identity 建立新 Incident。`incident_alerts`、`incident_assignments` 與 `incident_status_history` 保存關聯 alert、指派與狀態變更。每個 `rca_runs` 可擁有專家執行、hypothesis 與版本化報告；evidence 的實際資料位於月分割表。

```sql
CREATE TABLE incidents (
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
)

CREATE TABLE incident_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id UUID NOT NULL REFERENCES incidents(id),
    alert_event_id UUID NOT NULL,
    alert_event_partition_timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (alert_event_id, alert_event_partition_timestamp)
        REFERENCES alert_events(id, partition_timestamp),
    UNIQUE (incident_id, alert_event_id, alert_event_partition_timestamp)
)

CREATE TABLE incident_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id UUID NOT NULL REFERENCES incidents(id),
    assignee_id UUID NOT NULL REFERENCES subjects(id),
    assigned_by UUID REFERENCES subjects(id),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    unassigned_at TIMESTAMPTZ
)

CREATE TABLE incident_status_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id UUID NOT NULL REFERENCES incidents(id),
    from_status TEXT CHECK (from_status IN ('OPEN', 'INVESTIGATING', 'RESOLVED')),
    to_status TEXT NOT NULL CHECK (to_status IN ('OPEN', 'INVESTIGATING', 'RESOLVED')),
    changed_by UUID REFERENCES subjects(id),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason TEXT
)

CREATE TABLE rca_runs (
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
)

CREATE TABLE specialist_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rca_run_id UUID NOT NULL REFERENCES rca_runs(id),
    specialist_type TEXT NOT NULL CHECK (specialist_type IN ('METRICS', 'TRACES', 'LOGS')),
    status TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED')),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_message TEXT,
    UNIQUE (rca_run_id, specialist_type)
)

CREATE TABLE rca_hypotheses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rca_run_id UUID NOT NULL REFERENCES rca_runs(id),
    statement TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)

CREATE TABLE hypothesis_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hypothesis_id UUID NOT NULL REFERENCES rca_hypotheses(id),
    evidence_id UUID NOT NULL,
    evidence_partition_timestamp TIMESTAMPTZ NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('SUPPORTS', 'CONTRADICTS', 'MISSING')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (evidence_id, evidence_partition_timestamp)
        REFERENCES evidence_records(id, partition_timestamp),
    UNIQUE (hypothesis_id, evidence_id, evidence_partition_timestamp, relation)
)

CREATE TABLE rca_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rca_run_id UUID NOT NULL REFERENCES rca_runs(id),
    version INTEGER NOT NULL CHECK (version > 0),
    summary TEXT NOT NULL,
    report JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (rca_run_id, version)
)
```

### 非同步工作與事件發布

`outbox_events` 以 idempotency key 確保事件發布可重試；`worker_jobs` 與 `worker_attempts` 記錄 RCA 背景工作及每次執行嘗試。同一 RCA run 的同一 `job_type` 只能建立一筆 job，避免併發排程重複派送。

```sql
CREATE TABLE outbox_events (
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
)

CREATE TABLE worker_jobs (
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
)

CREATE TABLE worker_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    worker_job_id UUID NOT NULL REFERENCES worker_jobs(id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    UNIQUE (worker_job_id, attempt_number)
)

```

## 月分割資料表

以下六張是 partition parent。delivery、alert 與 evidence 的主要欄位分別為接收時間、觀測時間與 RCA run；Incident 訊息、時間線與 audit 使用建立／發生時間作為工作流與稽核記錄。所有 parent 都按 `partition_timestamp` 做 RANGE 分割。

```sql
CREATE TABLE webhook_deliveries (
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
) PARTITION BY RANGE (partition_timestamp)

CREATE TABLE alert_events (
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
) PARTITION BY RANGE (partition_timestamp)

CREATE TABLE evidence_records (
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
) PARTITION BY RANGE (partition_timestamp)

CREATE TABLE incident_messages (
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
) PARTITION BY RANGE (partition_timestamp)

CREATE TABLE incident_timeline_events (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    partition_timestamp TIMESTAMPTZ NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    incident_id UUID NOT NULL REFERENCES incidents(id),
    event_type TEXT NOT NULL,
    actor_id UUID REFERENCES subjects(id),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, partition_timestamp)
) PARTITION BY RANGE (partition_timestamp)

CREATE TABLE audit_events (
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
) PARTITION BY RANGE (partition_timestamp)
```

## 約束與索引

上方 DDL 的 `PRIMARY KEY`、`FOREIGN KEY`、`CHECK` 與 `UNIQUE` 是資料完整性的來源。以下是 migration 建立的 13 個具名 B-tree 索引。`uq_rca_runs_active_incident` 是 partial unique index：同一 Incident 在 `WAITING_FOR_CLASSIFICATION`、`QUEUED` 或 `RUNNING` 狀態僅能有一個活動 RCA run。`uq_incidents_active_identity` 同樣以 partial unique index 保證 canonical identity 在 `OPEN`、`INVESTIGATING` 之間合計只有一筆活動 Incident；`uq_worker_jobs_run_type` 則保證每個 `(rca_run_id, job_type)` 唯一。這些不變量都由資料庫在併發寫入時仲裁。

```sql
CREATE UNIQUE INDEX uq_rca_runs_active_incident
    ON rca_runs (incident_id)
    WHERE status IN ('WAITING_FOR_CLASSIFICATION', 'QUEUED', 'RUNNING')

CREATE UNIQUE INDEX uq_incidents_active_identity
    ON incidents (identity_key)
    WHERE status IN ('OPEN', 'INVESTIGATING')

CREATE UNIQUE INDEX uq_worker_jobs_run_type ON worker_jobs (rca_run_id, job_type)

CREATE INDEX ix_webhook_deliveries_source_received ON webhook_deliveries (source_id, received_at)
CREATE INDEX ix_alert_events_source_fingerprint_observed ON alert_events (source_id, fingerprint, observed_at)
CREATE INDEX ix_alert_instances_state_last_seen ON alert_instances (state, last_seen_at)
CREATE INDEX ix_incidents_scope_status_opened ON incidents (project_id, environment_id, status, opened_at)
CREATE INDEX ix_evidence_records_run_observed ON evidence_records (rca_run_id, observed_at)
CREATE INDEX ix_incident_messages_incident_created ON incident_messages (incident_id, created_at)
CREATE INDEX ix_incident_timeline_incident_occurred ON incident_timeline_events (incident_id, occurred_at)
CREATE INDEX ix_audit_events_resource_occurred ON audit_events (resource_type, resource_id, occurred_at)
CREATE INDEX ix_worker_jobs_status_available ON worker_jobs (status, available_at)
CREATE INDEX ix_outbox_events_status_available ON outbox_events (status, available_at)
```

## Partition 維護

allowlist 僅包含：`webhook_deliveries`、`alert_events`、`evidence_records`、`incident_messages`、`incident_timeline_events` 與 `audit_events`。migration upgrade 會建立當月與下個月的六張分區。執行期的 `ensure_monthly_partitions(connection, month)` 會將輸入月份正規化為該月第一天、依 allowlist 為每張表發出 `CREATE TABLE IF NOT EXISTS ... PARTITION OF`，因此可安全重複呼叫；它不會對 allowlist 之外的表建立分區。

每個月的上界是 exclusive，12 月的範例如下，資料時間剛好等於 `2032-01-01 00:00:00+00` 屬於下一個分區而非本分區：

```sql
CREATE TABLE webhook_deliveries_2031_12
PARTITION OF webhook_deliveries
FOR VALUES FROM ('2031-12-01 00:00:00+00')
TO ('2032-01-01 00:00:00+00');
```

## 驗證與故障排查

下列均為唯讀查詢，可用於確認已套用 migration 的 catalog 狀態：

```sql
-- 列出 RANGE partition parent 與分割策略。
SELECT c.relname AS table_name, p.partstrat
FROM pg_partitioned_table AS p
JOIN pg_class AS c ON c.oid = p.partrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
ORDER BY c.relname;

-- 檢視 parent 與每個實際月分區的繼承關係。
SELECT parent.relname AS parent_table, child.relname AS partition_table,
       pg_get_expr(child.relpartbound, child.oid) AS partition_bound
FROM pg_inherits
JOIN pg_class AS child ON child.oid = inhrelid
JOIN pg_class AS parent ON parent.oid = inhparent
JOIN pg_namespace AS n ON n.oid = parent.relnamespace
WHERE n.nspname = 'public'
  AND parent.relkind = 'p'
ORDER BY parent.relname, child.relname;

-- 檢視目前 public schema 的索引定義。
SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY tablename, indexname;

-- 檢視欄位型別、可否為 NULL 與預設值。
SELECT table_name, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position;
```
