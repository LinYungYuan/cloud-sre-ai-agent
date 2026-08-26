# Backend Runtime 簡化與非分區資料表設計

**日期：** 2026-08-26

**狀態：** 已核准，待 implementation plan

**範圍：** `backend/`、`rca-worker/`、Backend/Worker migrations、Docker Compose 與 Kubernetes manifests

## 1. 摘要

目前 Backend API、Outbox Worker、Partition Worker 與 RCA Worker 是四個獨立程序。
Backend API 的設定模型仍要求未實際使用的 AI/MCP 參數；六張高寫入量資料表採月份
partition，因而需要常態維運 Partition CronJob。Outbox Worker 則持續掃描
`outbox_events`，自動發布 `PENDING`／`FAILED` 事件。

本次變更會：

1. 移除獨立 Outbox Worker。Backend API 在 request transaction commit 後，只嘗試發布
   該次 request 建立的 outbox event；Backend 啟動或運行期間均不自動掃描舊 backlog。
2. 保留 durable `outbox_events`，並提供明確的人工重送 CLI。漏發或失敗事件只由管理者
   手動恢復。
3. 將六張 partitioned tables 轉成普通 PostgreSQL tables，移除 Partition Worker 與
   Kubernetes CronJob。
4. 將 runtime、migration 與 Compose 設定拆成五份完全隔離的環境檔。
5. 從 Backend API 移除 AI／MCP 設定；這些設定只屬於 RCA Worker。

## 2. 目標

- Backend request 的業務資料、RCA job 與 outbox event 維持同一個 DB transaction。
- transaction commit 後立即嘗試發布該筆 event，但 publish 失敗不得回滾已 commit 的資料。
- Backend restart 不自動重送任何既有 `PENDING`／`FAILED` event。
- 提供可稽核、冪等且不能改寫 payload 的人工 Outbox recovery CLI。
- 移除獨立 Outbox Deployment、entrypoint 與設定檔。
- 將所有 partitioned tables 轉成普通表並完整保留資料、constraints、indexes 與關聯。
- 移除 Partition Worker、entrypoint、CronJob 與設定檔。
- 每個 runtime/migration 僅讀取自己被指定的環境檔，不跨檔案 fallback 或合併。
- OS／Kubernetes environment 必須覆蓋本機環境檔，production 不依賴 repository 檔案。
- Backend API 不再要求 `MODEL_NAME` 或任何 MCP endpoint。

## 3. 非目標

- 不在 Backend startup 自動修復 outbox backlog。
- 不在 Backend runtime 定期輪詢舊 outbox backlog。
- 不承諾 Pub/Sub exactly-once；系統維持 at-least-once 與既有 durable job/idempotency 防線。
- 不讓人工重送工具修改 topic、payload、aggregate、resource ID 或 idempotency key。
- 不修改 RCA Worker 的 ADK Specialist/Root Agent、安全界線或 evidence contract。
- 不在本次加入歷史資料 retention、archive 或 table sharding。
- 不共用單一 root `.env`。

## 4. 目標服務架構

```text
Grafana request
  -> Backend API
       -> DB transaction
            -> alert / incident state
            -> RCA run + worker job
            -> outbox event (PENDING)
       -> COMMIT
       -> publish only this outbox event
            -> success: PUBLISHED
            -> failure: FAILED, wait for manual recovery
  -> Pub/Sub
  -> RCA Worker
       -> MCP collection
       -> Metrics / Trace / Log ADK Specialists
       -> Root RCA Agent
       -> report persistence

Operator
  -> manual Outbox recovery CLI
       -> exact event or explicit PENDING/FAILED batch
       -> Pub/Sub
```

移除後不再存在：

```text
Outbox Worker Deployment
Partition Worker / CronJob
automatic startup backlog replay
automatic periodic backlog polling
```

## 5. Backend commit 後發布語意

### 5.1 Transaction boundary

Backend 必須在單一 transaction 內完成：

1. 驗證並保存 webhook delivery 與 alert/incident state。
2. 建立 RCA run 與 worker job。
3. 建立具固定 `idempotency_key` 的 `outbox_events` row，初始狀態為 `PENDING`。
4. commit。

任何 transaction 內錯誤都必須 rollback，且不得發布 Pub/Sub。

### 5.2 Post-commit publish

commit 成功後，同一個 Backend request flow 立即嘗試發布剛建立的 event ID；不得掃描或
順帶發布其他 event。

- publish 成功：將該 row 更新成 `PUBLISHED` 並保存 `published_at`。
- publish 失敗：將該 row更新成 `FAILED`，保留原 payload 與固定 idempotency key。
- publish 失敗不回滾業務 transaction，也不刪除 RCA job。
- webhook 仍回傳 `202 Accepted` 與既有 `deliveryId`，避免上游因 5xx 重送已 commit 的告警。
- 失敗細節不得回傳給 webhook caller；只保存 stable failure metadata/operational log。

若 Backend 在 commit 後、publish 前中斷，event 保持 `PENDING`。若在 Pub/Sub 接受訊息後、
DB 標記 `PUBLISHED` 前中斷，人工重送可能產生重複訊息；既有 Pub/Sub at-least-once、固定
idempotency key、unique RCA run/job 與 durable claim 規則必須安全吸收重複 delivery。

### 5.3 明確禁止自動 replay

Backend lifespan 不得：

- 在 startup 查詢 `PENDING`／`FAILED`。
- 建立定時 outbox polling task。
- 在新 request publish 時順便掃描歷史 backlog。

這是已核准的操作政策：中斷或失敗後由管理者決定何時重送。

## 6. 人工 Outbox recovery

提供內部 CLI，不新增公開 HTTP endpoint：

```bash
sre-agent-retry-outbox --event-id EVENT_UUID
sre-agent-retry-outbox --all-pending
sre-agent-retry-outbox --all-failed
```

CLI 要求：

- 三種 selector 互斥；未指定或同時指定多種時 fail closed。
- `--event-id` 只允許 `PENDING`／`FAILED`；`PUBLISHED` 回報 no-op 並以成功碼結束。
- batch selector 依 `available_at, created_at, id` 穩定排序，使用有界 batch size。
- 使用 `FOR UPDATE SKIP LOCKED`，可由多位管理者安全並行執行。
- 只能發布 DB 中已保存的 canonical payload、topic 與 idempotency key。
- 不接受任意 message body、topic、project、subscription 或 attribute override。
- 成功更新 `PUBLISHED/published_at`；失敗保持/更新為 `FAILED`。
- 輸出 event ID、原狀態、結果與 stable failure category，不輸出秘密或完整 payload。
- 操作必須留下 audit record，包含 operator identity、執行時間、selector 與結果計數。

## 7. Outbox 元件調整

保留：

- `outbox_events` table 與 idempotency constraint。
- canonical `RCA_RUN_REQUESTED` serialization。
- Google Pub/Sub publisher boundary。
- 單筆與有界 batch 的 publish/settlement primitive。

移除：

- `sre-agent-outbox-worker` 常駐 polling entrypoint。
- Backend outbox Deployment、service account/resource configuration（若只供該 Deployment）。
- README 的獨立 Outbox 啟動步驟。
- `.env.outbox` 與 `.env.outbox.example`。

既有 `OutboxPublisher` 應拆成可重用的「指定 event publish」與「明確 selector batch recovery」
application service；Backend request 與 CLI 共用相同 persistence/publisher primitive，避免兩套
settlement 規則。

## 8. 普通非分區資料表

以下 tables 改為普通 tables：

- `webhook_deliveries`
- `alert_events`
- `evidence_records`
- `incident_messages`
- `incident_timeline_events`
- `audit_events`

### 8.1 目標 schema

- 每張表使用 `id UUID PRIMARY KEY`，不再使用 `(id, partition_timestamp)` composite PK。
- 移除僅為 partition routing/複合外鍵存在的 `partition_timestamp` 與
  `*_partition_timestamp` helper columns。
- 保留具業務語意的 `received_at`、`observed_at`、`occurred_at`、`created_at` 等時間欄位。
- child tables 改用 UUID-only foreign keys。
- 重建既有 uniqueness、check constraints 與 query indexes。
- 為主要時間查詢保留/新增普通 B-tree indexes；不預先導入新的 archive/retention 系統。

### 8.2 Forward migration

Backend migration 必須在受控 maintenance window 執行：

1. 確認 migration 前備份可用，停止所有會寫入相關 tables 的 runtime。
2. 建立具目標普通 schema 的 replacement tables。
3. 依 dependency order 複製所有 parent partitions 的資料。
4. 驗證每張表 row count、UUID uniqueness、非空欄位、foreign keys 與關鍵 aggregate counts。
5. 重建 indexes、constraints、sequences/defaults 與 grants。
6. 在短 transaction 內重新命名舊 parent/replacement tables，切換 canonical names。
7. 再次驗證 schema、資料筆數、foreign keys 與代表性 read/write queries。
8. 驗證期內保留 renamed legacy partition tables；正式驗收與備份確認後，才由明確的後續
   cleanup migration 移除，不在第一次轉換中直接刪除。

migration 不得以 truncate/recreate 空表取代資料搬移，也不得在未驗證 row counts 前刪除任何
legacy partition 或 child table。

### 8.3 Rollback

這是 forward schema conversion，不提供可在有新 writes 後無損自動 downgrade 的 Alembic
rollback。若切換後驗證失敗，在尚未接受新 writes 的 maintenance window 內可交換回 legacy
tables；若已接受新 writes，必須停止流量、搬移 delta 或由已驗證備份恢復，不得直接執行
破壞性 downgrade。

## 9. Partition 元件移除

移除：

- `sre-agent-ensure-partitions` entrypoint。
- `PartitionWorkerSettings` 與 partition maintenance runtime。
- `ensure_monthly_partitions` 及只為 runtime partition creation 存在的 helper。
- Kubernetes `partition-cronjob.yaml` 及相關 overlays/RBAC。
- Partition Worker tests、README 與 runbook 指令。
- `.env.partition-worker` 與 `.env.partition-worker.example`。

migration history 不重寫；已發布 migration 保持 immutable，新的 forward migration 完成 schema
轉換。

## 10. 隔離環境設定檔

只存在以下五個本機環境檔：

```text
.env.backend-api
.env.rca-worker
.env.backend-migration
.env.rca-worker-migration
.env.compose
```

每份檔案具同名 `.example` committed template；實際檔案必須由 `.gitignore` 排除。每個
entrypoint 僅載入自己被指定的檔案，不讀取其他檔案，不使用共享 root `.env`，也不合併
多份 dotenv。OS environment 覆蓋檔案值。

Production image 不內含實際 `.env.*`；Kubernetes 繼續以 Secret、ConfigMap 與 Workload
Identity 注入 OS environment。

### 10.1 `.env.compose`

```dotenv
POSTGRES_DB=sre_agent
POSTGRES_USER=postgres
POSTGRES_HOST_PORT=5432
PUBSUB_PROJECT_ID=sre-agent-local
PUBSUB_HOST_PORT=58085
```

啟動時明確指定：

```bash
docker compose --env-file .env.compose up -d postgres pubsub-emulator
```

### 10.2 `.env.backend-api`

```dotenv
DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent
GRAFANA_TOKENS={...}
APP_ENVIRONMENT=local
WEBHOOK_MAX_BODY_BYTES=1048576
PUBSUB_EMULATOR_HOST=127.0.0.1:58085
PUBSUB_PROJECT_ID=sre-agent-local
RCA_TOPIC_ID=rca-jobs
```

Backend API 不得包含或要求：

```text
MODEL_NAME
METRICS_MCP_URL
TRACE_MCP_URL
LOG_MCP_URL
MCP_CAPABILITY_MANIFEST
SPECIALIST_ANALYSIS_MODE
任何 evidence/tool/agent budget
```

### 10.3 `.env.rca-worker`

```dotenv
DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent
PUBSUB_EMULATOR_HOST=127.0.0.1:58085
PUBSUB_PROJECT_ID=sre-agent-local
PUBSUB_AUTO_CREATE=true
RCA_TOPIC_ID=rca-jobs
PUBSUB_SUBSCRIPTION_ID=rca-jobs-local-sub
APP_ENVIRONMENT=local
MODEL_NAME=gemini-2.5-flash
METRICS_MCP_URL=https://localhost.invalid/metrics/mcp
TRACE_MCP_URL=https://localhost.invalid/traces/mcp
LOG_MCP_URL=https://localhost.invalid/logs/mcp
MCP_CAPABILITY_MANIFEST=[]
SPECIALIST_ANALYSIS_MODE=DISABLED
MCP_MAX_RESPONSE_BYTES=2097152
EVIDENCE_CHUNK_CHARS=8000
EVIDENCE_MAX_CHUNKS=4
EVIDENCE_MAX_TOTAL_CHARS=32000
SPECIALIST_MAX_TOOL_CALLS=5
SPECIALIST_MAX_OBSERVATIONS=20
RCA_DEADLINE_SECONDS=300
AGENT_CORRECTIVE_RETRIES=1
```

### 10.4 Migration files

`.env.backend-migration` 與 `.env.rca-worker-migration` 各自只包含該 migration environment
需要的 DB connection 與必要 Alembic controls。兩者即使連向同一 database，也各自保存
connection string，不互相引用。

範例命令必須明確選擇設定檔；不得依賴 shell 先前殘留的 `DATABASE_URL`。

## 11. 設定載入與安全

- 每個 executable entrypoint 使用固定 default filename，並允許一個該程序專屬的
  env-file path override；禁止通用 fallback chain。
- loader 以 `override=False` 載入，使 Secret/ConfigMap 注入的 OS variables 優先。
- 固定 default file 存在時只載入該檔；production 可不包含檔案而完全依賴 OS environment。
- 若明確指定程序專屬 env-file override，該檔缺失必須 fail closed；未提供檔案且 OS
  environment 缺必要欄位時，由 Pydantic 在 startup/migration 前 fail closed。
- Pydantic `extra="forbid"` 保留；因每份檔案只含該程序欄位，不需放寬為 `ignore`。
- validation error 不輸出 secret values。
- `.example` 只能包含 local placeholder，不得包含 production tokens、credentials 或 MCP secrets。

## 12. Deployment 變更

- Backend Deployment 增加 Pub/Sub project/topic 與必要 identity permission，因 Backend 現在
  直接發布新 event。
- 移除 Outbox Deployment。
- 移除 Partition CronJob 與僅供其使用的設定/RBAC。
- RCA Worker Deployment 保留 AI、MCP、Pub/Sub subscriber 與 DB settings。
- Backend Deployment 移除 `MODEL_NAME`、三個 MCP URL 與所有 RCA analysis budgets。
- Backend `Settings` 同時移除未使用的 `rca_deadline_seconds`；job deadline 只由 RCA Worker
  的 `RCA_DEADLINE_SECONDS` 執行。
- Pub/Sub IAM 維持 Backend publisher、RCA Worker subscriber 的最小權限分離。

## 13. 可觀測性與操作

Backend 必須提供不含 payload/secret 的 structured events：

- post-commit publish attempted/succeeded/failed。
- event ID、delivery ID、stable failure category 與 latency。
- 不自動處理的 `PENDING`／`FAILED` backlog count 應由監控 query/metric 顯示，但不得觸發
  自動 replay。

Manual CLI 必須輸出選取、成功、失敗、no-op 數量，並以非零 exit status 表示任何未完成
publish。Runbook 必須說明如何先 dry-run/query backlog，再執行指定 selector。

## 14. 測試與驗收

### 14.1 Outbox

- transaction rollback 時不發布且不建立可重送 event。
- commit 後只發布該 request 的 event，不處理歷史 backlog。
- publish 成功標記 `PUBLISHED`。
- publish 失敗標記 `FAILED`，webhook 維持既有 accepted contract。
- Backend startup 有既存 `PENDING`／`FAILED` 時不發布。
- Backend runtime 沒有 periodic backlog polling。
- manual exact/batch selector、`PUBLISHED` no-op、stable ordering、concurrency 與 payload immutability。
- crash-window duplicate delivery 不建立第二個 RCA run/job。

### 14.2 Schema conversion

- 在 PostgreSQL integration test 建立跨多個月份的 parent/child fixtures。
- upgrade 後六張 canonical tables 均不是 partitioned relations。
- 所有 row counts、UUIDs、business timestamps、foreign keys 與 representative reads 保持一致。
- upgrade 後可寫入任意月份資料，不需先建立 partition。
- legacy tables 在首次 migration 後仍保留且不再接收 writes。
- cleanup migration 只在顯式驗收 gate 後測試/執行。

### 14.3 Environment isolation

- 每個 entrypoint 只載入自己的檔案。
- 明確指定的 env file 缺失時 fail closed；未提供檔案且 OS environment 完整時可啟動。
- 檔案與 OS environment 都缺必要值時 fail closed。
- OS environment 覆蓋 dotenv。
- Backend API 在沒有 AI/MCP variables 時可啟動。
- RCA Worker 缺必要 model/MCP settings 時依 rollout contract 驗證。
- migration A 不會讀取 migration B 的設定檔。
- `.env.*` 實際檔案不會被 Git 追蹤，所有 `.example` 不含秘密。

### 14.4 Repository verification

- Backend、RCA Worker、contract 與 PostgreSQL integration suites 全部通過。
- Ruff format/check 與 Pyright 零錯誤。
- Kubernetes/Compose render 不再包含 Outbox Deployment 或 Partition CronJob。
- 搜尋確認 Backend production code 不再宣告或讀取 AI/MCP settings。
- 搜尋確認沒有 runtime partition creation 或自動 outbox backlog replay。

## 15. 實作順序約束

1. 先完成 environment isolation 與 Backend AI/MCP setting cleanup。
2. 將 Outbox publish primitive 改成 request-scoped/manual-scoped，再整合 Backend commit 後流程。
3. 完成人工 recovery CLI 與 audit/observability。
4. 在 PostgreSQL integration coverage 下完成普通表 forward migration。
5. 移除 Partition runtime/CronJob 與 Outbox Deployment。
6. 更新 Compose、Kubernetes、README/runbook，最後做完整驗證與 rollout gate。

不得在普通表 migration 通過資料保存驗證前移除 Partition Worker，也不得在 Backend
request-scoped publish 與 manual recovery 都可用前移除獨立 Outbox Worker deployment。
