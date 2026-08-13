# PostgreSQL Schema 參考

## 適用範圍與權威來源

### RCA Worker migration 順序與資料相容性

部署順序固定為 **Backend migration → RCA Worker migration**。Backend 將版本寫入
`alembic_version_backend`；其到達 `0002_grafana_normalization_v2` 後，Worker 才能
將自己的版本寫入 `alembic_version_rca_worker`。兩者使用同一 application role，
但仍由 table ownership contract 限制各自可修改的資料表。

Worker 接管 legacy RCA tables 後，`worker_jobs` 以 60 秒 lease 與最多 3 次 attempt
控制 at-least-once delivery；`rca_runs`、`specialist_runs`、`worker_attempts` 只保存
allowlisted `failure_code`，不保存可能包含 credential 的 exception 文字。Evidence
保存 `raw_result BYTEA`、`structured_data JSONB`、`metadata JSONB`、SHA-256
`content_hash` 與 provenance。

Worker migration 的 downgrade 會捨棄新格式的 evidence bytes 與 metadata，並只能寫入
明確的 legacy marker；被捨棄的精確 evidence **無法還原**。因此 production rollback
前必須先備份，不能把 downgrade 當作無損轉換。

本文件是目前 SRE Agent 告警接收、Incident、RCA 與稽核資料模型的**閱讀與審查參考**，適用於 PostgreSQL 18。現有可執行 baseline 是 Backend Alembic revision `0001_alert_incident_schema`；本文 SQL 不可直接當作 migration 執行，也不取代 `alembic upgrade` 或 `alembic downgrade`。修改 migration 的資料表、約束、索引或分割邏輯時，必須在同一變更同步更新本文件。

核准的拆包目標是 Backend 與 RCA Worker 各自管理 migration：Backend 使用 `alembic_version_backend`，RCA Worker 使用 `alembic_version_rca_worker`，新環境依序套用 Backend、再套用 Worker。`0001_alert_incident_schema` 是拆包前的 legacy baseline，已建立 core 與部分 RCA tables；拆包實作會移轉 Alembic version-table metadata，而不重新執行 baseline DDL。後續 Backend 不得修改 Worker-owned schema，Worker migration 也不得修改 Alert／Incident core schema。

Backend、RCA Worker 與兩套 Alembic migrations 共用同一個 application role；Angular 不連 PostgreSQL。共用 role 具備應用程式 DML 與 migrations 所需 DDL，但不具 superuser、role management、database owner 或本系統以外 schema 的權限。目標 ownership 如下；實作完成後以 `contracts/database/table-ownership.yaml` 的 machine-readable migration contract 為準：

- Backend DDL owner：scope/source、webhook delivery、alert、Incident、timeline、outbox、audit 與 Operator API 所需 core tables。
- RCA Worker DDL owner：RCA run、specialist run、evidence、hypothesis、report、worker job 與 attempt tables。
- `incident_messages` 是 Backend-owned legacy-reserved table，本期不提供聊天功能。
- Backend production code 只讀寫 core tables 及原子排程所需的 Worker tables；RCA Worker production code 只讀 core context 並寫入 Worker-owned tables 與明確允許的 audit records。
- 資料庫不以不同 login role 強制套件隔離；Backend 與 Worker migration ownership 由分開的 migration 目錄、version tables、compatibility tests 與 code review 強制。

## 本機啟動與 migration

在 repository 根目錄以本機開發設定啟動 PostgreSQL 18：

```sh
docker compose up -d postgres
docker compose exec postgres pg_isready -U postgres -d sre_agent
cd backend
UV_CACHE_DIR=.uv-cache uv run alembic upgrade head
```

`docker-compose.yml` 的本機資料庫是 `sre_agent`、使用者 `postgres`，透過 passwordless 的 `postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent` 連線。Compose 僅將 PostgreSQL 綁定在 loopback，且 `trust` 驗證僅限本機開發；不得複製到正式環境。若要在本機重建至 migration 前狀態，執行下列指令。**警告：`alembic downgrade base` 會刪除本 schema 的資料表與其中所有資料，只能用於可丟棄的本機資料庫。**

```sh
cd backend
UV_CACHE_DIR=.uv-cache uv run alembic downgrade base
UV_CACHE_DIR=.uv-cache uv run alembic upgrade head
```

## 共同規則

- 識別碼以 `UUID` 表示，預設由 `gen_random_uuid()` 生成。
- 所有事件與生命週期時間使用 UTC 的 `TIMESTAMPTZ`；不要以無時區 timestamp 解讀資料。
- 半結構化欄位使用 `JSONB`。`webhook_deliveries.raw_body BYTEA` 永久保存 webhook delivery 的精確 request bytes（含空白、key 順序、escape、數字字面值與重複 JSON key），`body_hash` 是這組精確 bytes 的 SHA-256；`webhook_deliveries.raw_payload` 另外保存解析後、可查詢的 JSONB。`alert_events.raw_payload` 只保存該筆 alert 的 JSONB，不重複整份 webhook bytes。這些 immutable 原始紀錄不應覆寫或刪除以取代處理後資料。
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
    UNIQUE (team_id, name),
    UNIQUE (team_id, id)
)

CREATE TABLE environments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name),
    UNIQUE (project_id, id)
)

CREATE TABLE services (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id UUID NOT NULL REFERENCES environments(id),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (environment_id, name),
    UNIQUE (environment_id, id)
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
    UNIQUE (project_id, environment_id, name),
    FOREIGN KEY (project_id, environment_id)
        REFERENCES environments(project_id, id)
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
    CHECK (num_nonnulls(team_id, project_id, environment_id, service_id) >= 1),
    CHECK (team_id IS NULL OR environment_id IS NULL OR project_id IS NOT NULL),
    CHECK (project_id IS NULL OR service_id IS NULL OR environment_id IS NOT NULL),
    CHECK (team_id IS NULL OR service_id IS NULL OR
           (project_id IS NOT NULL AND environment_id IS NOT NULL)),
    FOREIGN KEY (team_id, project_id) REFERENCES projects(team_id, id),
    FOREIGN KEY (project_id, environment_id)
        REFERENCES environments(project_id, id),
    FOREIGN KEY (environment_id, service_id)
        REFERENCES services(environment_id, id)
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (team_id, project_id) REFERENCES projects(team_id, id),
    FOREIGN KEY (project_id, environment_id)
        REFERENCES environments(project_id, id),
    FOREIGN KEY (environment_id, service_id)
        REFERENCES services(environment_id, id)
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
    raw_body BYTEA NOT NULL,
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
    CHECK (time_window_end >= time_window_start),
    CHECK (team_id IS NULL OR environment_id IS NULL OR project_id IS NOT NULL),
    CHECK (project_id IS NULL OR service_id IS NULL OR environment_id IS NOT NULL),
    CHECK (team_id IS NULL OR service_id IS NULL OR
           (project_id IS NOT NULL AND environment_id IS NOT NULL)),
    FOREIGN KEY (team_id, project_id) REFERENCES projects(team_id, id),
    FOREIGN KEY (project_id, environment_id)
        REFERENCES environments(project_id, id),
    FOREIGN KEY (environment_id, service_id)
        REFERENCES services(environment_id, id)
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

## Grafana 正規化與 Incident identity v2（revision 0002）

Backend migration 從本 revision 起使用獨立的 `alembic_version_backend`；若部署中仍有舊的 `alembic_version`，migration 啟動時會先在同一資料庫安全重新命名並保留 revision。Backend 與 RCA Worker 的 migration source ownership 不同，但三個應用程式共用同一個 PostgreSQL application role。

`folder_code` 是 Grafana 的專案／系統代碼，**folder_code is not projects.id**。它只用於 identity v2 與可選的 `folder_scope_mappings`；找不到 mapping 時，Incident 的 team、project、environment、service 可以全部為 `NULL`，仍然必須建立 RCA。provider 只看 alert labels 是否存在 `resource.label.project_id`，mapping 不得改寫 provider。

以下是 forward-only migration 的兩張 catalog 表完整 DDL：

```sql
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
    CONSTRAINT ck_normalization_rules_provider CHECK (provider IN ('GCP', 'AWS')),
    CONSTRAINT ck_normalization_rules_conditions_array
        CHECK (jsonb_typeof(conditions) = 'array'),
    CONSTRAINT ck_normalization_rules_output_object
        CHECK (jsonb_typeof(output) = 'object')
);

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
);
```

既有 parent table 的 forward-only ALTER 如下。`alert_events` 與 `incidents` 的 canonical 欄位允許 `NULL`，用來保留 revision 0001 的歷史資料；新寫入由 application 明確提供 identity version 2 與正規化欄位。

```sql
ALTER TABLE webhook_deliveries
    ADD COLUMN truncated_alerts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN incomplete BOOLEAN NOT NULL DEFAULT false,
    ADD CONSTRAINT ck_webhook_deliveries_truncated_alerts
        CHECK (truncated_alerts >= 0);

ALTER TABLE alert_events
    ADD COLUMN provider TEXT NULL,
    ADD COLUMN folder_code TEXT NULL,
    ADD COLUMN alert_name TEXT NULL,
    ADD COLUMN severity_raw TEXT NULL,
    ADD COLUMN severity_canonical TEXT NULL,
    ADD COLUMN issue JSONB NULL,
    ADD COLUMN resource JSONB NULL,
    ADD COLUMN normalization_status TEXT NOT NULL DEFAULT 'UNCLASSIFIED',
    ADD COLUMN normalization_rule_id UUID NULL REFERENCES normalization_rules(id),
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
        CHECK (normalization_status IN ('NORMALIZED', 'UNCLASSIFIED', 'VALIDATION_FAILED')),
    ADD CONSTRAINT ck_alert_events_normalization_warnings_array
        CHECK (jsonb_typeof(normalization_warnings) = 'array'),
    ADD CONSTRAINT ck_alert_events_rule_reference
        CHECK ((normalization_rule_id IS NULL) = (normalization_rule_version IS NULL));

ALTER TABLE incidents
    ADD COLUMN identity_version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN provider TEXT NULL,
    ADD COLUMN folder_code TEXT NULL,
    ADD COLUMN alert_name TEXT NULL,
    ALTER COLUMN team_id DROP NOT NULL,
    ALTER COLUMN project_id DROP NOT NULL,
    ALTER COLUMN environment_id DROP NOT NULL,
    DROP CONSTRAINT incidents_severity_check,
    ADD CONSTRAINT ck_incidents_identity_version CHECK (identity_version IN (1, 2)),
    ADD CONSTRAINT ck_incidents_provider
        CHECK (provider IS NULL OR provider IN ('GCP', 'AWS')),
    ADD CONSTRAINT ck_incidents_severity_v2
        CHECK (severity IN ('SEV1', 'SEV2', 'SEV3', 'SEV4', 'UNMAPPED'));

DROP INDEX uq_incidents_active_identity;
CREATE UNIQUE INDEX uq_incidents_active_identity
    ON incidents (identity_version, identity_key)
    WHERE status IN ('OPEN', 'INVESTIGATING');
CREATE INDEX ix_normalization_rules_lookup ON normalization_rules (source_id, enabled, priority);
CREATE INDEX ix_folder_scope_mappings_lookup ON folder_scope_mappings (source_id, enabled, folder_code);
```

Downgrade 警告：從 revision 0002 降回 0001 會永久刪除 normalization rules、folder mappings 與所有 canonical normalization 欄位；如果已存在 nullable scope 或 `UNMAPPED` Incident，恢復舊的 `NOT NULL`／severity constraint 前必須先修復資料。正式環境不應把 downgrade 當成一般 rollback 策略。

## Partition 維護

allowlist 僅包含：`webhook_deliveries`、`alert_events`、`evidence_records`、`incident_messages`、`incident_timeline_events` 與 `audit_events`。migration upgrade 會建立當月與下個月的六張分區。執行期的 `ensure_monthly_partitions(connection, month)` 使用明確的 `public` schema 建立月分區，建立後逐一驗證 `relispartition`、正確 parent 與精確 bounds；同名 ordinary table、掛錯 parent 或錯誤 bounds 都會以 partition drift 失敗，不會被 `IF NOT EXISTS` 靜默略過。

獨立維護命令 `sre-agent-ensure-partitions`（亦可執行 `python -m sre_agent.workers.partition_worker`）在啟動時才讀取 `DATABASE_URL`，建立本月、下月與下下月的安全 runway，失敗時回傳非零 exit code；排程與 infrastructure provisioning 不屬於此命令。`DATABASE_URL` 格式為 `postgresql://user:password@host:port/database` 或 SQLAlchemy 的 `postgresql+asyncpg://...`，設定使用 secret type 且命令不輸出 URL 或其中 credential。

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
