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
2. 保留 durable `outbox_events`，並提供受保護的人工重送 REST API。漏發或失敗事件只由
   管理者手動恢復。
3. 將六張 partitioned tables 轉成普通 PostgreSQL tables，移除 Partition Worker 與
   Kubernetes CronJob。
4. 將 runtime、migration 與 Compose 設定拆成五份完全隔離的環境檔。
5. 從 Backend API 移除 AI／MCP 設定；這些設定只屬於 RCA Worker。

## 2. 目標

- Backend request 的業務資料、RCA job 與 outbox event 維持同一個 DB transaction。
- transaction commit 後立即嘗試發布該筆 event，但 publish 失敗不得回滾已 commit 的資料。
- Backend restart 不自動重送任何既有 `PENDING`／`FAILED` event。
- 提供可稽核、冪等且不能改寫 payload 的人工 Outbox recovery REST API。
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
  -> protected manual Outbox recovery REST API
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

提供 Backend 內部管理 REST API，不提供 recovery CLI：

```http
POST /api/v1/operations/outbox-events/{eventId}/retry
POST /api/v1/operations/outbox-events/retry-pending?limit=100
POST /api/v1/operations/outbox-events/retry-failed?limit=100
```

API 要求：

- endpoint 只允許具 Outbox recovery 權限的管理者或內部 service account 呼叫；未驗證與
  未授權分別回傳 `401`／`403`。
- `{eventId}` 只允許 `PENDING`／`FAILED`；`PUBLISHED` 回傳 `200` no-op。
- 不存在的 event 回傳 `404`，不得洩漏 payload 或其他 tenant/scope 資訊。
- batch endpoint 的 `limit` 必須限制在 `1..100`；未提供時使用固定 server default。
- batch selector 依 `available_at, created_at, id` 穩定排序。
- 使用 `FOR UPDATE SKIP LOCKED`，可由多位管理者安全並行執行。
- 只能發布 DB 中已保存的 canonical payload、topic 與 idempotency key。
- endpoint 不接受 message body，也不接受任意 topic、project、subscription、payload 或
  attribute override。
- 成功更新 `PUBLISHED/published_at`；失敗保持/更新為 `FAILED`。
- response 只輸出 event ID、原狀態、結果、selected/published/failed/no-op counts 與 stable
  failure category，不輸出秘密或完整 payload。
- 操作必須留下 audit record，包含 operator identity、執行時間、selector 與結果計數。
- 單筆與批次操作同步完成有界 publish 後回傳 `200`；部分 publish 失敗仍回傳結果摘要，並
  以 response schema 的 `failed` count 與 stable categories 表達，不使用未定義的 5xx 重試
  語意。

單筆 response：

```json
{
  "eventId": "00000000-0000-0000-0000-000000000000",
  "previousStatus": "FAILED",
  "result": "PUBLISHED"
}
```

批次 response：

```json
{
  "selected": 10,
  "published": 8,
  "failed": 2,
  "noOp": 0
}
```

Backend 本身不可用時無法執行 recovery API；必須先恢復 Backend。這是已核准的服務生命
週期與人工恢復政策。

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
application service；Backend request 與 recovery API 共用相同 persistence/publisher primitive，
避免兩套 settlement 規則。

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

已發布的 Backend 與 RCA Worker migration history 保持 immutable。唯一支援的 schema rollout
順序固定為下列四個 revision gate：

1. Backend 到 `0002_grafana_normalization_v2`。
2. RCA Worker 到 `0002_adk_specialist_analysis`。
3. Backend 到 `0003_non_partition_runtime_tables`。
4. RCA Worker 到新的 `0003` post-conversion revision。

每個命令都必須指定明確 revision；禁止在任何階段用 `upgrade head` 越過尚未驗證的中間 gate。
兩個 Alembic version tables 必須在每階段前後分別驗證，且不得以 stamp 或 migration environment
內的條件分支偽造已執行的 revision。production 四階段 transition 必須位於同一個受控
maintenance window：在第一個尚未完成的 gate 前停止所有 writes，直到第四個 gate 與最終 schema
驗證完成後才可切換 runtime。clean database 雖無既有 writes，仍必須通過相同 preconditions 與
postconditions。

#### 8.2.1 前兩個 immutable gates

Backend `0002` 先建立既有 partitioned baseline，Worker `0001`、`0002` 再依其已發布內容接管
Worker-owned tables，加入 durable lifecycle、exact evidence 與 specialist analysis schema。第二個
gate 完成時，conversion 的來源必須是完整 Worker `0002_adk_specialist_analysis` schema，而不是
僅有 Backend `0002` 的 legacy baseline。

全新 database 與已存在且達 Worker `0002` head 的 database 使用同一組四階段契約。全新 database
依序執行四個明確 target；既有 Worker-head database 必須驗證前兩個 target 與 schema 已完整達成，
然後只執行第三、第四階段，不重演 migration、重新 stamp 或採用另一條捷徑。每一階段重跑時，
version table 已在該 target 且 postcondition schema 仍通過驗證，才可成為 Alembic no-op；
revision/schema 不一致必須 fail closed。

#### 8.2.2 Backend `0003` ordinary-table conversion

Backend `0003_non_partition_runtime_tables` 必須在受控 maintenance window 內，並以 Worker `0002`
schema 為唯一合法來源執行：

1. 確認 migration 前備份可用，停止 Backend、RCA Worker 與所有會寫入相關 tables 的 runtime。
2. 驗證 Backend version 為 `0002_grafana_normalization_v2`、Worker version 為
   `0002_adk_specialist_analysis`，並驗證 Worker-owned columns/constraints 符合來源契約。
3. 建立具目標普通 schema 的 replacement tables。
4. 依 dependency order 複製所有 parent partitions 的資料。
5. 驗證每張表 row count、UUID uniqueness、非空欄位、foreign keys 與關鍵 aggregate counts。
6. 重建 indexes、constraints、sequences/defaults 與 grants。
7. 在短 transaction 內重新命名舊 parent/replacement tables，切換 canonical names。
8. 再次驗證 schema、資料筆數、foreign keys 與代表性 read/write queries。
9. 驗證期內保留 renamed legacy partition tables；正式驗收與備份確認後，才由明確的後續
   cleanup migration 移除，不在第一次轉換中直接刪除。

replacement schema 與 copy 必須完整保存 Worker `0002` 已擁有的資料與欄位，包含
`evidence_records.raw_result BYTEA`、`metadata JSONB`、`content_hash`，以及
`rca_reports.result_status` 和所有 Worker lifecycle/specialist analysis 欄位。除將六張 canonical
tables 改為 UUID-only ordinary tables 所必須移除的 `partition_timestamp` 與
`*_partition_timestamp` helpers 外，不得刪除、降級或重新解釋 Worker schema。exact MCP bytes 與
provenance metadata 不得轉成 synthetic hash pointer 或其他不可還原的 `raw_result_reference`。
Backend `0003` 未 replacement 的 Worker-owned tables 及其 lifecycle/analysis columns 必須原樣
保留；若 replacement 或 FK rebuild 觸及 Worker-owned table，copy/constraint contract 也必須以
Worker `0002` schema 為準。

Backend `0003` 不得以 truncate/recreate 空表取代資料搬移，也不得在未驗證 row counts 前刪除
任何 legacy partition 或 child table。

#### 8.2.3 Worker `0003` post-conversion gate

新的 Worker `0003` 是 Backend conversion 後的 validation/ownership gate。它必須要求 Backend
version 已為 `0003_non_partition_runtime_tables`、Worker version 正好為
`0002_adk_specialist_analysis`，驗證 ordinary UUID-only schema 與 Worker `0002` 資料仍完整，
並只建立必要的 UUID-only Worker constraints/indexes。它不得重演、改寫或複製 Worker `0001`、
`0002` 的 lifecycle、raw evidence、report status 或 analysis migration。

Backend 與 RCA Worker runtime 只有在 Backend version 為
`0003_non_partition_runtime_tables` 且 Worker version 為新 `0003`，且兩個 post-migration schema
checks 均成功後，才可在同一 maintenance window 切換為 UUID-only runtime。不得在兩個 migration
targets 之間啟動任一新 runtime，也不得讓 composite-reference 舊 runtime 對 ordinary tables
恢復寫入。

### 8.3 Rollback

四個階段各自在執行前驗證兩個 revision tables 與 schema preconditions；任何 revision 跳躍、
unexpected column/constraint、copy mismatch 或 validation failure 都必須 fail closed，保持 runtimes
停止並由 operator 判斷，不得自動 stamp、略過或繼續下一階段。

Backend `0003` 是 forward schema conversion，不提供可在有新 writes 後無損自動 downgrade 的
Alembic rollback。renamed legacy partition tables 必須保留。若切換後驗證失敗，在尚未接受新
writes 的 maintenance window 內可依已驗證 procedure 交換回 legacy tables；若已接受新 writes，
必須停止流量、搬移 delta 或由已驗證備份恢復，不得直接執行破壞性 downgrade。Worker 新
`0003` 的 validation/constraint 失敗也不得嘗試重寫 Worker `0001`、`0002` history。

### 8.4 明確拒絕的 migration 方案

- 不修改、squash 或重建已發布的 Worker `0001_rca_worker_v1` 或
  `0002_adk_specialist_analysis`。
- 不在 migration environment 依目前 schema 做 conditional stamp、跳過 revision body，或讓
  `upgrade head` 隱式跨越中間 gate。
- 不因 ordinary-table conversion 丟棄 `raw_result`、`metadata`、`result_status`、lifecycle 或
  analysis data，也不以 synthetic pointer 取代 exact raw bytes/provenance。
- 不為 clean database 與 existing Worker-head database 維護兩套 migration history 或不同最終
  schema。

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

Recovery API 必須回傳選取、成功、失敗與 no-op 數量；OpenAPI contract 必須固定 response
schema 與 authorization errors。Runbook 必須說明如何先查詢 backlog，再呼叫指定 endpoint，
且不得將 API 暴露為未受保護的 public route。

## 14. 測試與驗收

### 14.1 Outbox

- transaction rollback 時不發布且不建立可重送 event。
- commit 後只發布該 request 的 event，不處理歷史 backlog。
- publish 成功標記 `PUBLISHED`。
- publish 失敗標記 `FAILED`，webhook 維持既有 accepted contract。
- Backend startup 有既存 `PENDING`／`FAILED` 時不發布。
- Backend runtime 沒有 periodic backlog polling。
- recovery REST API authorization、OpenAPI schema、exact/batch selector、`PUBLISHED` no-op、
  stable ordering、concurrency 與 payload immutability。
- crash-window duplicate delivery 不建立第二個 RCA run/job。

### 14.2 Schema conversion

- PostgreSQL integration test 必須從 clean database 真實依序執行 Backend `0002`、Worker `0002`、
  Backend `0003`、Worker 新 `0003`；不得直接 stamp、只執行單一 stream 或以預建 final schema
  取代四階段 migration。
- 另以已達 Backend `0002`／Worker `0002` 的 existing Worker-head fixture 執行相同 gate contract，
  證明第三、第四階段得到與 clean database 相同的 version tables 與 schema。
- 在 conversion 前建立跨多個月份的 parent/child fixtures，包含 exact non-UTF-8/非 canonical JSON
  raw bytes、provenance metadata、`result_status` 與 specialist `analysis_result` 代表資料。
- upgrade 後六張 canonical tables 均不是 partitioned relations。
- 所有 row counts、UUIDs、business timestamps、foreign keys、exact raw bytes、metadata、content hash、
  report result status、Worker lifecycle 與 specialist analysis 欄位及 representative reads 保持一致。
- upgrade 後可寫入任意月份資料，不需先建立 partition。
- migration acceptance 必須以新 UUID-only Worker runtime 執行 evidence persistence 與 report
  persistence smoke test，證明 exact bytes/provenance 可讀回、hypothesis evidence UUID FK 生效，
  且 report `result_status` 正確保存。
- 每個中間 gate 都測試 wrong revision/schema 時 fail closed；重跑已完成 target 只允許 version-table
  一致的 Alembic no-op。
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
3. 完成人工 recovery REST API、authorization、OpenAPI contract 與 audit/observability。
4. 建立真實四階段 PostgreSQL integration coverage，並使 Backend `0003` 以 Worker `0002` schema
   保存全部資料完成 ordinary-table conversion。
5. 新增只負責 post-conversion validation 與必要 UUID-only constraints/indexes 的 Worker `0003`；
   不修改既有 migration history。
6. 完成 Backend 與 RCA Worker UUID-only runtime，但在兩個 migration streams 都到達第 8.2 節目標
   前不得部署或啟動。
7. 移除 Partition runtime/CronJob 與 Outbox Deployment。
8. 更新 Compose、Kubernetes、README/runbook，最後依第 8.2 節的四個明確 revision targets 執行
   maintenance-window rollout；禁止以 `upgrade head` 取代 gate commands。

不得在普通表 migration 通過資料保存驗證前移除 Partition Worker，也不得在 Backend
request-scoped publish 與 manual recovery 都可用前移除獨立 Outbox Worker deployment。正式 rollout
固定為 `Backend 0002` → `Worker 0002_adk_specialist_analysis` →
`Backend 0003_non_partition_runtime_tables` → `Worker 新 0003`；只有最後 gate 完成後，才同時切換
Backend/Worker UUID-only runtimes。
