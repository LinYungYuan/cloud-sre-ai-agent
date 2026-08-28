# Backend Runtime Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將 Outbox 發布責任整合進 Backend API、提供受保護的手動重送 REST API、移除資料表分區與 Partition Worker，並讓 Backend、RCA Worker 與兩組 migration 各自使用隔離的環境設定檔。

**Architecture:** Backend 在同一個資料庫 transaction 建立 RCA job 與 `PENDING` outbox event，commit 後只嘗試發布本次 request 新增的 event；歷史 `PENDING`／`FAILED` event 只能由具全域權限的 Operator 呼叫 recovery API 重送。六張分區表在 maintenance window 由 Backend migration 原子切換成 UUID 主鍵的普通表，Backend 與 RCA Worker 同步改用 UUID-only reference。RCA Worker 繼續獨立訂閱 Pub/Sub 並持有所有 AI／MCP 設定。

**Tech Stack:** Python 3.11、FastAPI、Pydantic Settings、SQLAlchemy asyncio、Alembic、PostgreSQL、Google Cloud Pub/Sub、pytest、OpenAPI、Kustomize、Docker Compose

**Spec:** `docs/superpowers/specs/2026-08-26-backend-runtime-simplification-design.md`

## Global Constraints

- Backend request transaction 必須同時保存業務資料、RCA run、worker job 與 outbox event；commit 失敗不得 publish。
- Commit 後的即時發布只能接收該 request 新建立的 event UUID，不得掃描、順帶處理或在 startup 自動重送歷史 backlog。
- Publish 失敗不得回滾已 commit 的 webhook；Grafana endpoint 仍回 `202 Accepted`，event 必須落為 `FAILED`。
- 手動 recovery API 不接受 payload、topic、project、subscription、attribute 或其他發布內容覆寫。
- Recovery API 沿用 `OperatorIdentityProvider`，且只有 `identity.global_access is True` 可執行；未驗證為 `401`、已驗證但非全域權限為 `403`、event 不存在為 `404`。
- `PUBLISHED` event 的單筆重送是 idempotent no-op，回 `200` 且不得再次 publish。
- 批次重送限制 `1..100`，以 `available_at, created_at, id` 穩定排序並使用 `FOR UPDATE SKIP LOCKED`。
- Pub/Sub delivery 維持 at-least-once；Worker 的 durable claim 與 idempotency 不得弱化。
- 六張 canonical table 最終必須是普通表與 `id UUID PRIMARY KEY`；所有 partition helper columns 與複合 FK 必須從應用介面、SQL、schema 與文件移除。
- Initial migration 必須保留重新命名後的 legacy partition tables；不得在同一 release 自動刪除，也不得宣稱 downgrade 可在新寫入後無損完成。
- `.env.backend-api`、`.env.rca-worker`、`.env.backend-migration`、`.env.rca-worker-migration`、`.env.compose` 彼此隔離；OS environment 優先，明確指定但不存在的 override path 必須 fail closed。
- 不建立 `.env.outbox`、`.env.partition-worker` 或共用 `.env`。
- 真實 `.env.*` 不得提交；只提交各自的 `.example`。
- Backend 不得持有 `MODEL_NAME`、Metrics／Trace／Log MCP URL、MCP manifest、specialist mode、evidence budgets 或 RCA deadline；這些只屬於 RCA Worker。
- 每個 task 完成後只 commit 該 task 的檔案，不得混入 workspace 內既有或其他 task 的修改。

## Target File Map

### New files

- `.env.backend-api.example`
- `.env.rca-worker.example`
- `.env.backend-migration.example`
- `.env.rca-worker-migration.example`
- `.env.compose.example`
- `backend/src/sre_agent/config/env_files.py`
- `backend/src/sre_agent/application/outbox/__init__.py`
- `backend/src/sre_agent/application/outbox/publish_events.py`
- `backend/src/sre_agent/application/outbox/recover_events.py`
- `backend/src/sre_agent/api/routers/outbox_operations.py`
- `backend/src/sre_agent/api/schemas/outbox_operations.py`
- `backend/migrations/versions/0003_non_partition_runtime_tables.py`
- `backend/tests/unit/config/test_env_files.py`
- `backend/tests/unit/application/outbox/test_publish_events.py`
- `backend/tests/unit/application/outbox/test_recover_events.py`
- `backend/tests/contract/api/test_outbox_operations.py`
- `backend/tests/integration/application/outbox/test_publish_events.py`
- `backend/tests/integration/persistence/test_non_partition_migration.py`
- `rca-worker/src/sre_rca_worker/config/env_files.py`
- `rca-worker/tests/unit/config/test_env_files.py`

### Modified files

- `.gitignore`, `docker-compose.yml`, `README.md`, `backend/README.md`, `rca-worker/README.md`
- `backend/pyproject.toml`, `rca-worker/pyproject.toml`
- `backend/src/sre_agent/config/settings.py`, `rca-worker/src/sre_rca_worker/config/settings.py`
- `backend/src/sre_agent/api/main.py`, `backend/src/sre_agent/api/composition.py`, `backend/src/sre_agent/api/dependencies.py`, `backend/src/sre_agent/api/error_handlers.py`
- `backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py`
- `backend/src/sre_agent/persistence/repositories/alerts.py`, `incidents.py`, `jobs.py`, `operator_reads.py`
- `backend/migrations/env.py`, `rca-worker/migrations/env.py`
- `backend/tests/**` 與 `rca-worker/tests/**` 中所有 partition reference fixture／assertion
- `rca-worker/src/sre_rca_worker/domain/evidence/models.py`
- `rca-worker/src/sre_rca_worker/persistence/repositories/rca.py`
- `rca-worker/src/sre_rca_worker/application/rca/processor.py`
- `rca-worker/src/sre_rca_worker/agents/rca/adk_agent.py`, `synthesizer.py`
- `rca-worker/src/sre_rca_worker/agents/specialists/validator.py`
- `contracts/openapi/operator-api-v1.yaml`
- `contracts/compatibility-tests/test_contracts.py`, `test_design_consistency.py`, `test_gke_manifests.py`
- `deploy/k8s/base/backend-deployment.yaml`, `configmap.yaml`, `kustomization.yaml`, `serviceaccounts.yaml`
- `deploy/k8s/jobs/backend-migration-job.yaml`, `worker-migration-job.yaml`, `deploy/k8s/README.md`
- `docs/database/postgresql-schema.md`

### Deleted files

- `backend/src/sre_agent/config/outbox_settings.py`
- `backend/src/sre_agent/workers/outbox_main.py`
- `backend/src/sre_agent/workers/outbox_worker.py`
- `backend/src/sre_agent/workers/partition_worker.py`
- `backend/src/sre_agent/persistence/database.py`
- `backend/tests/unit/config/test_outbox_settings.py`
- `backend/tests/unit/workers/test_outbox_main.py`
- `backend/tests/unit/workers/test_partition_worker.py`
- `backend/tests/integration/workers/test_outbox_worker.py`
- `backend/tests/integration/workers/test_partition_worker_integration.py`
- `deploy/k8s/base/outbox-deployment.yaml`
- `deploy/k8s/base/partition-cronjob.yaml`

---

## Task 1: 建立五組隔離的環境檔載入契約

**Files:**

- Create: `.env.backend-api.example`
- Create: `.env.rca-worker.example`
- Create: `.env.backend-migration.example`
- Create: `.env.rca-worker-migration.example`
- Create: `.env.compose.example`
- Create: `backend/src/sre_agent/config/env_files.py`
- Create: `backend/tests/unit/config/test_env_files.py`
- Create: `rca-worker/src/sre_rca_worker/config/env_files.py`
- Create: `rca-worker/tests/unit/config/test_env_files.py`
- Modify: `.gitignore`
- Modify: `backend/pyproject.toml`
- Modify: `rca-worker/pyproject.toml`

- [ ] **Step 1: 先寫 Backend 與 Worker env-path resolver 的 failing tests**

  測試固定預設檔名、檔案不存在時回 `None`、`BACKEND_ENV_FILE`／`RCA_WORKER_ENV_FILE` 指定存在檔案時採用該 path、明確指定不存在檔案時拋 `FileNotFoundError`。同時測試 resolver 不讀取另一個程式的 override variable。

- [ ] **Step 2: 執行 RED 測試**

  Run: `cd backend && uv run pytest tests/unit/config/test_env_files.py -v && cd ../rca-worker && uv run pytest tests/unit/config/test_env_files.py -v`

  Expected: collection 因 `sre_agent.config.env_files` 與 `sre_rca_worker.config.env_files` 尚不存在而失敗。

- [ ] **Step 3: 實作相同但不共用的 resolver**

  Backend 公開：

  ```python
  def resolve_backend_env_file(
      environ: Mapping[str, str] = os.environ,
      cwd: Path | None = None,
  ) -> Path | None: ...
  ```

  Worker 公開：

  ```python
  def resolve_worker_env_file(
      environ: Mapping[str, str] = os.environ,
      cwd: Path | None = None,
  ) -> Path | None: ...
  ```

  預設只檢查 cwd 下的 `.env.backend-api` 或 `.env.rca-worker`。Override variable 一旦存在就必須解析成明確 path，檔案不存在立即失敗。加入直接 dependency `python-dotenv`，供兩組 Alembic task 後續使用。

- [ ] **Step 4: 建立五個完整 example 並強化 ignore**

  每個 example 只能含該程式所需 keys；`.gitignore` 忽略 `.env.backend-api`、`.env.rca-worker`、`.env.backend-migration`、`.env.rca-worker-migration`、`.env.compose`，但以 negation 保留五個 `.example`。Backend example 不得含 AI/MCP key；migration example 只含 `DATABASE_URL`；compose example 只含 host ports、database bootstrap 與 emulator project。

- [ ] **Step 5: 執行 GREEN 測試與 secret scan**

  Run: `cd backend && uv run pytest tests/unit/config/test_env_files.py -v && cd ../rca-worker && uv run pytest tests/unit/config/test_env_files.py -v && cd .. && git check-ignore .env.backend-api .env.rca-worker .env.backend-migration .env.rca-worker-migration .env.compose`

  Expected: tests PASS，五個真實檔名全部被 ignore，五個 `.example` 可被 Git 追蹤。

- [ ] **Step 6: Commit**

  ```bash
  git add .gitignore .env.backend-api.example .env.rca-worker.example .env.backend-migration.example .env.rca-worker-migration.example .env.compose.example backend/pyproject.toml backend/src/sre_agent/config/env_files.py backend/tests/unit/config/test_env_files.py rca-worker/pyproject.toml rca-worker/src/sre_rca_worker/config/env_files.py rca-worker/tests/unit/config/test_env_files.py
  git commit -m "feat: isolate runtime environment files"
  ```

## Task 2: 縮減 Backend settings，保留 Worker 專屬 AI/MCP settings

**Files:**

- Modify: `backend/src/sre_agent/config/settings.py`
- Modify: `backend/src/sre_agent/api/main.py`
- Modify: `backend/tests/unit/config/test_settings.py`
- Modify: `backend/tests/contract/api/test_app_composition.py`
- Modify: `backend/tests/contract/api/test_health.py`
- Modify: `rca-worker/src/sre_rca_worker/config/settings.py`
- Modify: `rca-worker/src/sre_rca_worker/workers/rca_worker.py`
- Modify: `rca-worker/tests/unit/config/test_settings.py`

- [ ] **Step 1: 寫 settings isolation failing tests**

  Backend `Settings` 的合法欄位固定為 `database_url`, `grafana_tokens`, `pubsub_project_id`, `rca_topic_id`, `pubsub_emulator_host`, `app_environment`, `webhook_max_body_bytes`；傳入 `MODEL_NAME` 或任何 MCP/budget/deadline key 必須因 `extra="forbid"` 失敗。Worker 必須繼續接受並驗證現有 AI/MCP/budget keys。兩者都測試 OS environment 覆蓋 env-file value。

- [ ] **Step 2: 執行 RED 測試**

  Run: `cd backend && uv run pytest tests/unit/config/test_settings.py tests/contract/api/test_app_composition.py tests/contract/api/test_health.py -v && cd ../rca-worker && uv run pytest tests/unit/config/test_settings.py -v`

  Expected: Backend 仍要求 `MODEL_NAME` 與三個 MCP URL，且 settings 尚未載入隔離 env file。

- [ ] **Step 3: 實作 runtime settings loading**

  Backend `_load_settings()` 使用 `Settings(_env_file=resolve_backend_env_file(), _env_file_override=False)` 的等價 Pydantic 呼叫；Worker startup 使用 `resolve_worker_env_file()`。不得在 module import 時讀檔。若 Pydantic 版本沒有 `_env_file_override`，以既有 priority（init > environment > dotenv）實作並用測試鎖住 OS env 優先。

- [ ] **Step 4: 移除 Backend AI/MCP fields 並加入 Pub/Sub emulator safety**

  Backend `Settings` 移除 `model_name`, `metrics_mcp_url`, `trace_mcp_url`, `log_mcp_url`, `rca_deadline_seconds`；加入 `pubsub_emulator_host: str | None = None`，production 若設定 emulator 必須 fail closed。Worker fields 與 validation 不變。

- [ ] **Step 5: 更新所有 Backend test setting factories**

  刪除測試資料中的 Backend AI/MCP keys，加入必要 Pub/Sub publisher keys；保留測試對 unknown key 的拒絕驗證。

- [ ] **Step 6: 執行 GREEN 測試**

  Run: `cd backend && uv run pytest tests/unit/config/test_settings.py tests/contract/api/test_app_composition.py tests/contract/api/test_health.py -v && cd ../rca-worker && uv run pytest tests/unit/config/test_settings.py -v`

  Expected: PASS。

- [ ] **Step 7: Commit**

  ```bash
  git add backend/src/sre_agent/config/settings.py backend/src/sre_agent/api/main.py backend/tests/unit/config/test_settings.py backend/tests/contract/api/test_app_composition.py backend/tests/contract/api/test_health.py rca-worker/src/sre_rca_worker/config/settings.py rca-worker/src/sre_rca_worker/workers/rca_worker.py rca-worker/tests/unit/config/test_settings.py
  git commit -m "refactor: separate backend and worker settings"
  ```

## Task 3: 讓兩組 Alembic migration 使用各自環境檔

**Files:**

- Modify: `backend/migrations/env.py`
- Modify: `backend/tests/integration/persistence/test_alembic_version_reconciliation.py`
- Modify: `rca-worker/migrations/env.py`
- Modify: `rca-worker/tests/integration/persistence/test_schema.py`

- [ ] **Step 1: 寫 migration env loader failing tests**

  Backend 只認 `BACKEND_MIGRATION_ENV_FILE` 或預設 `.env.backend-migration`；Worker 只認 `RCA_WORKER_MIGRATION_ENV_FILE` 或預設 `.env.rca-worker-migration`。兩者使用 `load_dotenv(..., override=False)`，所以已存在的 OS `DATABASE_URL` 必須勝過檔案。保留 `MIGRATION_TEST_DATABASE_URL` 為 test-only 最高優先來源。

- [ ] **Step 2: 執行 RED 測試**

  Run: `cd backend && uv run pytest tests/integration/persistence/test_alembic_version_reconciliation.py -v && cd ../rca-worker && uv run pytest tests/integration/persistence/test_schema.py -v`

  Expected: 新增的 env-file isolation assertions 失敗。

- [ ] **Step 3: 實作兩組 migration resolver 與 dotenv loading**

  不可 import 對方 package；兩個 `migrations/env.py` 各自解析專屬 variable。Explicit override path 不存在時在建立 engine 前失敗，錯誤不得包含 connection string。

- [ ] **Step 4: 執行 GREEN 與 cross-contamination tests**

  Run: `cd backend && uv run pytest tests/integration/persistence/test_alembic_version_reconciliation.py -v && cd ../rca-worker && uv run pytest tests/integration/persistence/test_schema.py -v`

  Expected: PASS，Backend migration 不讀 Worker file，反之亦然。

- [ ] **Step 5: Commit**

  ```bash
  git add backend/migrations/env.py backend/tests/integration/persistence/test_alembic_version_reconciliation.py rca-worker/migrations/env.py rca-worker/tests/integration/persistence/test_schema.py
  git commit -m "feat: isolate migration environment files"
  ```

## Task 4: 將 Outbox 改成明確 event UUID 的發布服務

**Files:**

- Create: `backend/src/sre_agent/application/outbox/__init__.py`
- Create: `backend/src/sre_agent/application/outbox/publish_events.py`
- Create: `backend/tests/unit/application/outbox/test_publish_events.py`
- Create: `backend/tests/integration/application/outbox/test_publish_events.py`
- Modify: `backend/src/sre_agent/persistence/repositories/jobs.py`
- Modify: `backend/tests/integration/application/test_ingest_grafana_alerts.py`

- [ ] **Step 1: 定義並測試精確介面**

  ```python
  @dataclass(frozen=True, slots=True)
  class RcaWorkCreation:
      run_id: UUID
      outbox_event_id: UUID | None

  class PublishResultCode(StrEnum):
      PUBLISHED = "PUBLISHED"
      FAILED = "FAILED"
      NO_OP = "NO_OP"

  @dataclass(frozen=True, slots=True)
  class OutboxPublishResult:
      event_id: UUID
      previous_status: str
      result: PublishResultCode
      failure_category: str | None = None

  class OutboxEventNotFound(LookupError): ...

  class OutboxPublishService:
      async def publish_event(self, event_id: UUID) -> OutboxPublishResult: ...
      async def publish_pending(self, limit: int) -> tuple[OutboxPublishResult, ...]: ...
      async def publish_failed(self, limit: int) -> tuple[OutboxPublishResult, ...]: ...
  ```

  Unit tests 驗證 explicit UUID、`PUBLISHED` no-op、不支援 event type／壞 payload 回 `FAILED/INVALID_EVENT`、transport error 回 `FAILED/PUBLISH_ERROR`，兩類失敗都不得向 webhook caller 外拋；cancellation settlement 必須保留。Integration tests 驗證 row lock、穩定排序、status-specific batch 與 `SKIP LOCKED`。

- [ ] **Step 2: 執行 RED 測試**

  Run: `cd backend && uv run pytest tests/unit/application/outbox/test_publish_events.py tests/integration/application/outbox/test_publish_events.py -v`

  Expected: 新 module 不存在。

- [ ] **Step 3: 讓 JobRepository 回傳 event UUID**

  `create_rca_work(...) -> RcaWorkCreation`。建立新 run 時先產生 explicit `event_id = uuid4()` 並插入 `outbox_events.id`；existing active run 回 `RcaWorkCreation(existing_run_id, None)`，避免重複發布舊 event。若 idempotency conflict，查出既有 event ID，但只有本 transaction 確實新建 event 才回非 `None`。

- [ ] **Step 4: 實作 publish service**

  單筆 query 以 UUID 鎖 row；batch query 明確只選 `PENDING` 或只選 `FAILED`，並依 `available_at, created_at, id` 排序。共用私有 `_publish_locked_rows` 進行 canonical message、`asyncio.to_thread` publish 與 durable settlement。API-facing failure 只回 stable `INVALID_EVENT` 或 `PUBLISH_ERROR`，不得回 exception text。以 structured log 記錄 event ID、result、failure category 與 latency，但不得記錄 payload 或 credential。

- [ ] **Step 5: 執行 GREEN 測試**

  Run: `cd backend && uv run pytest tests/unit/application/outbox/test_publish_events.py tests/integration/application/outbox/test_publish_events.py tests/integration/application/test_ingest_grafana_alerts.py -v`

  Expected: PASS。

- [ ] **Step 6: Commit**

  ```bash
  git add backend/src/sre_agent/application/outbox backend/src/sre_agent/persistence/repositories/jobs.py backend/tests/unit/application/outbox backend/tests/integration/application/outbox backend/tests/integration/application/test_ingest_grafana_alerts.py
  git commit -m "refactor: publish explicit outbox events"
  ```

## Task 5: Backend commit 後立即發布本次 request 的 events

**Files:**

- Modify: `backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py`
- Modify: `backend/src/sre_agent/api/composition.py`
- Modify: `backend/src/sre_agent/integrations/pubsub/publisher.py`
- Modify: `backend/tests/integration/application/test_ingest_grafana_alerts.py`
- Modify: `backend/tests/integration/api/test_production_app_composition.py`
- Modify: `backend/tests/contract/api/test_grafana_webhook.py`

- [ ] **Step 1: 寫 post-commit ordering failing tests**

  Fake UoW 必須記錄 `commit`，fake publisher 必須斷言 publish 發生在 UoW `__aexit__` 完成後。覆蓋：新 incident 發布一筆；同 request 多個新 incident 各發布一筆；duplicate／existing active incident 不發布；commit failure 零發布；publish failure 仍回 ingestion result 且 HTTP 202；模擬 commit 後 response 前中斷再重送相同 webhook，不得建立第二個 RCA run/job/event。

- [ ] **Step 2: 執行 RED 測試**

  Run: `cd backend && uv run pytest tests/integration/application/test_ingest_grafana_alerts.py tests/contract/api/test_grafana_webhook.py -v`

  Expected: `IngestionResult` 沒有 event IDs 且 service 未注入 publisher。

- [ ] **Step 3: 擴充 ingestion 結果與流程**

  ```python
  @dataclass(frozen=True, slots=True)
  class IngestionResult:
      delivery_id: UUID
      accepted_at: datetime
      incident_ids: tuple[UUID, ...]
      outbox_event_ids: tuple[UUID, ...]
  ```

  Transaction 內只收集 `RcaWorkCreation.outbox_event_id`；離開 `async with` 後逐筆呼叫 `publish_event(event_id)`。Publish result 不改變 webhook response，也不得觸發歷史 batch method。以 structured event 記錄 delivery ID、event ID、attempted/succeeded/failed、stable failure category 與 latency，不得記錄 payload/token。

- [ ] **Step 4: 在 production resources 管理 Pub/Sub client lifecycle**

  Backend 建立 `PublisherClient`、topic path、`GooglePubSubPublisher` 與 `OutboxPublishService`；resource teardown 呼叫 `client.stop()`。Local emulator 由 `PUBSUB_EMULATOR_HOST` 交給 Google client library；production 已由 settings 禁止 emulator。Backend service account 只需 publisher 權限。

- [ ] **Step 5: 執行 GREEN 與 no-backlog regression**

  Run: `cd backend && uv run pytest tests/integration/application/test_ingest_grafana_alerts.py tests/integration/api/test_production_app_composition.py tests/contract/api/test_grafana_webhook.py -v`

  Expected: PASS；預先插入的歷史 `PENDING`／`FAILED` rows 在新 webhook 後保持未變。

- [ ] **Step 6: Commit**

  ```bash
  git add backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py backend/src/sre_agent/api/composition.py backend/src/sre_agent/integrations/pubsub/publisher.py backend/tests/integration/application/test_ingest_grafana_alerts.py backend/tests/integration/api/test_production_app_composition.py backend/tests/contract/api/test_grafana_webhook.py
  git commit -m "feat: publish new rca jobs after commit"
  ```

## Task 6: 提供全域 Operator 手動重送 REST API

**Files:**

- Create: `backend/src/sre_agent/application/outbox/recover_events.py`
- Create: `backend/tests/unit/application/outbox/test_recover_events.py`
- Create: `backend/src/sre_agent/api/routers/outbox_operations.py`
- Create: `backend/src/sre_agent/api/schemas/outbox_operations.py`
- Create: `backend/tests/contract/api/test_outbox_operations.py`
- Modify: `backend/src/sre_agent/api/main.py`
- Modify: `backend/src/sre_agent/api/composition.py`
- Modify: `backend/src/sre_agent/api/dependencies.py`
- Modify: `backend/src/sre_agent/api/error_handlers.py`
- Modify: `backend/src/sre_agent/application/outbox/publish_events.py`
- Modify: `contracts/openapi/operator-api-v1.yaml`
- Modify: `contracts/compatibility-tests/test_contracts.py`

- [ ] **Step 1: 寫三個 endpoint 的 contract tests**

  路徑固定為：

  - `POST /api/v1/operations/outbox-events/{eventId}/retry`
  - `POST /api/v1/operations/outbox-events/retry-pending?limit=100`
  - `POST /api/v1/operations/outbox-events/retry-failed?limit=100`

  測試無 request body、`limit` 1..100、camelCase response、correlation ID、PUBLISHED no-op 200、missing 404、無效 bearer 401、非 global identity 403、global identity 成功。Response 不得包含 payload、topic、project、subscription、attributes 或 exception message。

- [ ] **Step 2: 執行 RED 測試**

  Run: `cd backend && uv run pytest tests/contract/api/test_outbox_operations.py -v`

  Expected: routes 尚未註冊。

- [ ] **Step 3: 實作 authorization 與 schemas**

  共用既有 `OperatorIdentityProvider.resolve(Authorization)`。新增 `OperatorUnauthenticated`、`OperatorForbidden` 與對應 RFC 9457 handlers：可用 provider 缺少或拒絕 bearer 時回 401，identity 非 global 回 403。`UnavailableOperatorIdentityProvider` 代表整個 production identity integration 未設定，仍 fail closed 回 503；不得把 local identity 行為帶入 production。

- [ ] **Step 4: 實作 recovery application service、routes 與 audit**

  `OutboxRecoveryService` 接收 `OutboxPublishService` 與 audit repository；公開 `retry_event(event_id, identity, correlation_id)`、`retry_pending(limit, identity, correlation_id)`、`retry_failed(limit, identity, correlation_id)`。每次單筆／批次 recovery 都在 `audit_events` 寫入 action、actor external ID 對應 subject（存在時）或安全 scope metadata、event IDs、結果 counts 與 correlation ID；不寫 payload。Task 6 尚運行在 0002 schema，audit repository 暫以同一個 timezone-aware `occurred_at` 同值寫入 `partition_timestamp`；Task 8 必須連同 repository SQL 移除此 helper column。單筆 response 固定為 `eventId`, `previousStatus`, `result`, `failureCategory`；批次 response 固定為 `selected`, `published`, `failed`, `noOp`, `failureCategories`，其中 `failureCategories` 只使用 `INVALID_EVENT`、`PUBLISH_ERROR` 等 stable keys，不逐筆回傳 payload。另記錄不含 payload 的 request/result structured logs 與 batch counts。

- [ ] **Step 5: 更新 OpenAPI 並執行 GREEN**

  Run: `cd backend && uv run pytest tests/contract/api/test_outbox_operations.py -v && cd .. && uv run --project backend pytest contracts/compatibility-tests/test_contracts.py -v`

  Expected: PASS，OpenAPI validator 通過，API schema 無 override fields。

- [ ] **Step 6: Commit**

  ```bash
  git add backend/src/sre_agent/application/outbox/recover_events.py backend/src/sre_agent/application/outbox/publish_events.py backend/src/sre_agent/api/routers/outbox_operations.py backend/src/sre_agent/api/schemas/outbox_operations.py backend/src/sre_agent/api/main.py backend/src/sre_agent/api/composition.py backend/src/sre_agent/api/dependencies.py backend/src/sre_agent/api/error_handlers.py backend/tests/unit/application/outbox/test_recover_events.py backend/tests/contract/api/test_outbox_operations.py contracts/openapi/operator-api-v1.yaml contracts/compatibility-tests/test_contracts.py
  git commit -m "feat: add protected outbox recovery api"
  ```

## Task 7: 以 maintenance-window migration 將六張表切換成普通表

**Files:**

- Create: `backend/migrations/versions/0003_non_partition_runtime_tables.py`
- Create: `backend/tests/integration/persistence/test_non_partition_migration.py`
- Modify: `backend/tests/integration/persistence/test_schema.py`
- Modify: `backend/tests/unit/persistence/test_schema_documentation.py`

- [ ] **Step 1: 寫 migration acceptance tests**

  從 `0002_grafana_normalization_v2` 建立含跨月份資料的 schema，插入六張 partitioned tables 與所有依賴表的代表資料，再 upgrade 到 `0003`。驗證 canonical relations 的 `relkind='r'`、`relispartition=false`、UUID 單欄 PK、row counts 相等、UUID-only FKs 生效、partition helper columns 消失；legacy tables 固定命名為 `webhook_deliveries__partitioned_legacy_0003`、`alert_events__partitioned_legacy_0003`、`evidence_records__partitioned_legacy_0003`、`incident_messages__partitioned_legacy_0003`、`incident_timeline_events__partitioned_legacy_0003`、`audit_events__partitioned_legacy_0003`。

- [ ] **Step 2: 執行 RED 測試**

  Run: `cd backend && uv run pytest tests/integration/persistence/test_non_partition_migration.py -v`

  Expected: revision `0003_non_partition_runtime_tables` 不存在。

- [ ] **Step 3: 實作 replacement-table migration**

  在單一 maintenance transaction 中：建立六張 `_new` 普通表；移除 helper columns 並保留 `received_at`, `observed_at`, `created_at`, `occurred_at` 等業務時間；依 dependency order 複製資料；以 SQL assertions 驗證 source/target counts 與 UUID uniqueness；重建 dependent tables `ingestion_dedup_keys`, `alert_instances`, `incident_alerts`, `hypothesis_evidence` 為 UUID-only FK；重建 indexes/checks；將舊 canonical parents 原子 rename 為固定 legacy 名稱，再將 `_new` rename 成 canonical 名稱。

- [ ] **Step 4: 明確 downgrade policy**

  `downgrade()` 必須 raise 清楚的 `RuntimeError`，說明 ordinary-table 新寫入後無法保證 lossless downgrade。Migration 不得 drop legacy tables，也不得新增下一個會自動 cleanup 的 revision。

- [ ] **Step 5: 更新 schema tests**

  移除 `ensure_monthly_partitions` 與 partition-bound assertions，改驗證六張普通表、UUID-only references、所有既有 check/index 與 outbox indexes。加入 duplicate UUID migration 前置檢查失敗測試。

- [ ] **Step 6: 執行 GREEN**

  Run: `cd backend && uv run pytest tests/integration/persistence/test_non_partition_migration.py tests/integration/persistence/test_schema.py tests/unit/persistence/test_schema_documentation.py -v`

  Expected: PASS；legacy tables 存在，canonical tables 可正常寫入且不需建立月份 partition。

- [ ] **Step 7: Commit**

  ```bash
  git add backend/migrations/versions/0003_non_partition_runtime_tables.py backend/tests/integration/persistence/test_non_partition_migration.py backend/tests/integration/persistence/test_schema.py backend/tests/unit/persistence/test_schema_documentation.py
  git commit -m "feat: migrate runtime tables off partitioning"
  ```

## Task 8: 將 Backend repositories 改成 UUID-only references

**Files:**

- Modify: `backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py`
- Modify: `backend/src/sre_agent/persistence/repositories/alerts.py`
- Modify: `backend/src/sre_agent/persistence/repositories/incidents.py`
- Modify: `backend/src/sre_agent/persistence/repositories/operator_reads.py`
- Modify: `backend/src/sre_agent/application/outbox/recover_events.py`
- Modify: `backend/tests/unit/application/outbox/test_recover_events.py`
- Modify: `backend/tests/integration/application/test_ingest_grafana_alerts.py`
- Modify: `backend/tests/integration/api/test_operator_read_repository.py`
- Modify: `backend/tests/contract/api/test_grafana_webhook.py`
- Modify: `backend/tests/contract/api/test_operator_reads.py`

- [ ] **Step 1: 先把 fixtures 與介面 assertions 改成 UUID-only**

  `StoredAlertEvent` 改為只有 `id: UUID`。`claim_dedup_key` 移除 `delivery_partition_timestamp`；`finish_delivery` 移除 `partition_timestamp`；incident linking 與 operator joins 全部只用 event UUID。Recovery audit insert 同時移除 `partition_timestamp`，只保留 `occurred_at`。測試不得再插入或 assert helper columns。

- [ ] **Step 2: 執行 RED 測試**

  Run: `cd backend && uv run pytest tests/unit/application/outbox/test_recover_events.py tests/integration/application/test_ingest_grafana_alerts.py tests/integration/api/test_operator_read_repository.py tests/contract/api/test_grafana_webhook.py tests/contract/api/test_operator_reads.py -v`

  Expected: repository signatures 與 SQL 仍要求 partition timestamp。

- [ ] **Step 3: 最小化修改 Backend SQL**

  `webhook_deliveries` insert 不再寫 partition key；`alert_events.delivery_id` 直接 FK；`alert_instances.latest_event_id`、`incident_alerts.alert_event_id` 都只存 UUID；所有 update/select/join 移除 timestamp predicate。業務時間欄位和值不變。

- [ ] **Step 4: 執行 GREEN 與 static scan**

  Run: `cd backend && uv run pytest tests/unit/application/outbox/test_recover_events.py tests/integration/application/test_ingest_grafana_alerts.py tests/integration/api/test_operator_read_repository.py tests/contract/api/test_grafana_webhook.py tests/contract/api/test_operator_reads.py -v && ! rg "partition_timestamp|delivery_partition_timestamp|latest_event_partition_timestamp|alert_event_partition_timestamp" src/sre_agent`

  Expected: PASS 且 Backend runtime source 無 partition helper reference。

- [ ] **Step 5: Commit**

  ```bash
  git add backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py backend/src/sre_agent/application/outbox/recover_events.py backend/src/sre_agent/persistence/repositories/alerts.py backend/src/sre_agent/persistence/repositories/incidents.py backend/src/sre_agent/persistence/repositories/operator_reads.py backend/tests/unit/application/outbox/test_recover_events.py backend/tests/integration/application/test_ingest_grafana_alerts.py backend/tests/integration/api/test_operator_read_repository.py backend/tests/contract/api/test_grafana_webhook.py backend/tests/contract/api/test_operator_reads.py
  git commit -m "refactor: use uuid-only backend references"
  ```

## Task 9: 將 RCA Worker evidence 與 RCA 報告改成 UUID-only references

**Files:**

- Modify: `rca-worker/src/sre_rca_worker/domain/evidence/models.py`
- Modify: `rca-worker/src/sre_rca_worker/persistence/repositories/rca.py`
- Modify: `rca-worker/src/sre_rca_worker/application/rca/processor.py`
- Modify: `rca-worker/src/sre_rca_worker/agents/rca/adk_agent.py`
- Modify: `rca-worker/src/sre_rca_worker/agents/rca/synthesizer.py`
- Modify: `rca-worker/src/sre_rca_worker/agents/specialists/validator.py`
- Modify: `rca-worker/tests/contract/test_adk_specialist_boundaries.py`
- Modify: `rca-worker/tests/eval/test_rca_reports.py`
- Modify: `rca-worker/tests/integration/application/test_evidence_tools.py`
- Modify: `rca-worker/tests/integration/application/test_persist_evidence.py`
- Modify: `rca-worker/tests/integration/application/test_persist_report.py`
- Modify: `rca-worker/tests/integration/application/test_production_processor.py`
- Modify: `rca-worker/tests/unit/agents/rca/test_synthesizer.py`
- Modify: `rca-worker/tests/unit/agents/specialists/test_adk_agent.py`
- Modify: `rca-worker/tests/unit/agents/specialists/test_specialist_analysis_workflow.py`
- Modify: `rca-worker/tests/unit/agents/specialists/test_validator.py`
- Modify: `rca-worker/tests/unit/application/test_evidence_tool_session.py`
- Modify: `rca-worker/tests/unit/application/test_processor_retry.py`
- Modify: `rca-worker/tests/unit/domain/evidence/test_analysis.py`
- Modify: `rca-worker/tests/unit/domain/evidence/test_chunking.py`

- [ ] **Step 1: 將 domain tests 改成新 reference contract**

  ```python
  class EvidenceReference(BaseModel):
      model_config = ConfigDict(extra="forbid", frozen=True)
      id: UUID
  ```

  ADK tool/reference JSON 只接受例如 `{"id": "123e4567-e89b-12d3-a456-426614174000"}` 的單一 UUID 欄位；多餘 `partition_timestamp` 必須因 `extra="forbid"` 失敗。Known evidence 去重 key 改為 UUID。報告 evidence reference 只輸出 `evidenceId`，不再輸出 `partitionTimestamp`。

- [ ] **Step 2: 執行 RED 測試**

  Run: `cd rca-worker && uv run pytest tests/unit/domain/evidence tests/unit/agents/rca tests/unit/agents/specialists tests/unit/application -v`

  Expected: production models 與 validators 仍要求 composite reference。

- [ ] **Step 3: 更新 Worker persistence 與 processor SQL**

  `insert_evidence` 只寫 `observed_at` 並 `RETURNING id`；ownership、list/get、hypothesis link 與 alert-event joins 全部 UUID-only。移除 `AmbiguousEvidenceError`，因 UUID PK 已保證唯一。排序以 `observed_at, id` 取代 partition timestamp。

- [ ] **Step 4: 更新 agent validation 與 serialization**

  Synthesizer、specialist validator、ADK agent 的 set membership 與 schema field whitelist 只使用 UUID。Processor 的 report JSON 與 hypothesis insert 只傳 evidence UUID。不得保留相容性 alias，避免新舊 reference 混用。

- [ ] **Step 5: 更新全部 Worker fixtures 並執行 GREEN**

  Run: `cd rca-worker && uv run pytest tests/unit tests/contract tests/eval tests/integration/application -v && ! rg "partition_timestamp|partitionTimestamp|evidence_partition_timestamp|alert_event_partition_timestamp" src/sre_rca_worker`

  Expected: PASS 且 Worker runtime source 無 partition helper reference。

- [ ] **Step 6: Commit**

  ```bash
  git add rca-worker/src/sre_rca_worker rca-worker/tests
  git commit -m "refactor: use uuid-only evidence references"
  ```

## Task 10: 移除 Outbox Worker 與 Partition Worker runtime

**Files:**

- Delete: `backend/src/sre_agent/config/outbox_settings.py`
- Delete: `backend/src/sre_agent/workers/outbox_main.py`
- Delete: `backend/src/sre_agent/workers/outbox_worker.py`
- Delete: `backend/src/sre_agent/workers/partition_worker.py`
- Delete: `backend/src/sre_agent/persistence/database.py`
- Delete: `backend/tests/unit/config/test_outbox_settings.py`
- Delete: `backend/tests/unit/workers/test_outbox_main.py`
- Delete: `backend/tests/unit/workers/test_partition_worker.py`
- Delete: `backend/tests/integration/workers/test_outbox_worker.py`
- Delete: `backend/tests/integration/workers/test_partition_worker_integration.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/src/sre_agent/persistence/database.py`
- Modify: `contracts/compatibility-tests/test_design_consistency.py`

- [ ] **Step 1: 先更新 static contract tests**

  Tests 必須斷言 project scripts 不含 `sre-agent-outbox-worker` 與 `sre-agent-ensure-partitions`，runtime source 不含 polling loop、`ensure_monthly_partitions`、`PARTITIONED_TABLES` 或 standalone settings。

- [ ] **Step 2: 執行 RED 測試**

  Run: `uv run --project backend pytest contracts/compatibility-tests/test_design_consistency.py -v`

  Expected: 舊 entrypoints 與 modules 仍存在。

- [ ] **Step 3: 刪除 runtime 與舊 tests**

  移除兩個 console scripts、四個 worker/settings modules、純 partition 用途的 `persistence/database.py` 與其專屬 tests。不得刪除 Task 4 新的 explicit event publisher。

- [ ] **Step 4: 執行 GREEN 與 import scan**

  Run: `uv run --project backend pytest contracts/compatibility-tests/test_design_consistency.py -v && ! rg "outbox_main|OutboxSettings|ensure_monthly_partitions|PARTITIONED_TABLES|sre-agent-outbox-worker|sre-agent-ensure-partitions" backend/src backend/pyproject.toml`

  Expected: PASS 且無舊 runtime reference。

- [ ] **Step 5: Commit**

  ```bash
  git add -A backend/src/sre_agent backend/tests backend/pyproject.toml contracts/compatibility-tests/test_design_consistency.py
  git commit -m "refactor: remove outbox and partition workers"
  ```

## Task 11: 簡化 Compose 與 GKE deployment ownership

**Files:**

- Modify: `docker-compose.yml`
- Modify: `deploy/k8s/base/backend-deployment.yaml`
- Modify: `deploy/k8s/base/configmap.yaml`
- Modify: `deploy/k8s/base/kustomization.yaml`
- Modify: `deploy/k8s/base/serviceaccounts.yaml`
- Delete: `deploy/k8s/base/outbox-deployment.yaml`
- Delete: `deploy/k8s/base/partition-cronjob.yaml`
- Modify: `deploy/k8s/jobs/backend-migration-job.yaml`
- Modify: `deploy/k8s/jobs/worker-migration-job.yaml`
- Modify: `contracts/compatibility-tests/test_gke_manifests.py`

- [ ] **Step 1: 更新 deployment contract tests**

  Backend env 必須只有 Backend settings；Worker 保留 AI/MCP settings；兩個 migration job 只有各自 DB secret；kustomization 不得引用 Outbox Deployment 或 Partition CronJob；service accounts 不得保留 outbox account。Backend service account 必須標註/文件化 Pub/Sub publisher IAM，Worker 只需 subscriber。

- [ ] **Step 2: 執行 RED 測試**

  Run: `uv run --project backend pytest contracts/compatibility-tests/test_gke_manifests.py -v`

  Expected: Backend 尚含 AI/MCP env 且舊 workloads 仍存在。

- [ ] **Step 3: 更新 manifests**

  從 Backend Deployment 移除 AI/MCP/deadline keys，保留 DB、Grafana、app、webhook、Pub/Sub publisher keys。刪除 Outbox/Partition manifests 與 kustomization entries，移除 outbox service account。不得把 Worker secrets 掛到 Backend。

- [ ] **Step 4: 將 Compose interpolation 移到 `.env.compose`**

  `docker-compose.yml` 使用 `${POSTGRES_HOST_PORT:-5432}`、`${PUBSUB_HOST_PORT:-8085}`、`${POSTGRES_DB:-sre_agent}`、`${POSTGRES_USER:-postgres}`、`${PUBSUB_PROJECT_ID:-sre-agent-local}`；文件與測試命令固定使用 `docker compose --env-file .env.compose ...`。Compose file 不直接載入任何應用 env file。

- [ ] **Step 5: 執行 GREEN 與 render verification**

  Run: `uv run --project backend pytest contracts/compatibility-tests/test_gke_manifests.py -v && docker compose --env-file .env.compose.example config >/dev/null && kubectl kustomize deploy/k8s/base >/dev/null`

  Expected: PASS，render output 不含 outbox/partition workloads，Backend container 不含 AI/MCP env。

- [ ] **Step 6: Commit**

  ```bash
  git add -A docker-compose.yml deploy/k8s contracts/compatibility-tests/test_gke_manifests.py
  git commit -m "refactor: simplify runtime deployments"
  ```

## Task 12: 更新操作文件、資料庫文件與 rollout runbook

**Files:**

- Modify: `README.md`
- Modify: `backend/README.md`
- Modify: `rca-worker/README.md`
- Modify: `deploy/k8s/README.md`
- Modify: `docs/database/postgresql-schema.md`
- Modify: `docs/superpowers/specs/2026-08-26-backend-runtime-simplification-design.md`

- [ ] **Step 1: 寫文件 static assertions**

  在 `contracts/compatibility-tests/test_design_consistency.py` 加入：五個 `.env.*.example` 都被列出；startup 不自動 recovery；三個 recovery API 路徑存在；文件不再教使用 Outbox Worker／Partition Worker；Backend 設定表不含 AI/MCP；ordinary-table migration 明確要求 maintenance window；runbook 提供只讀 backlog count query，但該 query 不會觸發 replay。

- [ ] **Step 2: 執行 RED 測試**

  Run: `uv run --project backend pytest contracts/compatibility-tests/test_design_consistency.py -v`

  Expected: 舊文件仍描述 export 共用環境變數、outbox worker 與 partition command。

- [ ] **Step 3: 更新本機操作說明**

  文件提供：複製五個 example、以 `docker compose --env-file .env.compose up -d postgres pubsub-emulator` 啟動依賴、分別啟動 Backend/Worker、分別執行兩組 migration、以 REST API 手動重送。Runbook 先用 `SELECT status, count(*) FROM outbox_events WHERE status IN ('PENDING','FAILED') GROUP BY status ORDER BY status` 只讀查詢 backlog，再由管理者決定是否呼叫 recovery endpoint；不得提供 recovery CLI 或自動 replay。

- [ ] **Step 4: 更新 production rollout/runbook**

  固定順序：停止 Backend/Worker writes → 備份與確認空間 → 執行 Backend `0003` → 執行 Worker migration head → 同時部署 UUID-only Backend/Worker → smoke test webhook/publish/consume/report → 驗證 counts/FKs → 保留 legacy tables。Cleanup 必須是日後獨立變更與人工 acceptance gate；目前不提供自動 cleanup migration。

- [ ] **Step 5: 更新 spec 的權限細節**

  在既有設計文件補上 recovery API 沿用 `OperatorIdentityProvider` 且要求 `global_access`，與實作一致；不新增額外 shared token。

- [ ] **Step 6: 執行 GREEN 與 stale-text scan**

  Run: `uv run --project backend pytest contracts/compatibility-tests/test_design_consistency.py -v && ! rg "sre-agent-outbox-worker|sre-agent-ensure-partitions|\.env\.example" README.md backend/README.md rca-worker/README.md deploy/k8s/README.md docs/database/postgresql-schema.md`

  Expected: PASS；若需要提及舊名稱，只能出現在 migration history／removed-components 說明，並調整 scan allowlist，而非刪除必要歷史資訊。

- [ ] **Step 7: Commit**

  ```bash
  git add README.md backend/README.md rca-worker/README.md deploy/k8s/README.md docs/database/postgresql-schema.md docs/superpowers/specs/2026-08-26-backend-runtime-simplification-design.md contracts/compatibility-tests/test_design_consistency.py
  git commit -m "docs: document simplified rca runtime operations"
  ```

## Task 13: 全系統驗證與 release gate

**Files:** None（此 task 只驗證；若發現缺陷，回到擁有該檔案的 Task 1–12 修正、重跑該 task 的 RED/GREEN 並使用該 task 的 commit 範圍。）

- [ ] **Step 1: 建立乾淨測試資料庫並跑兩組 migration**

  Run: `docker compose --env-file .env.compose.example up -d postgres pubsub-emulator`

  Run: `cd backend && BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade head`

  Run: `cd rca-worker && RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade head`

  Expected: 兩組 version tables 到 head；六張 canonical table 都是 ordinary relations；legacy `__partitioned_legacy_0003` tables 存在。

- [ ] **Step 2: 跑 Backend 全測試與 static checks**

  Run: `cd backend && uv run pytest tests -v && uv run ruff check . && uv run pyright`

  Expected: 全部 PASS。

- [ ] **Step 3: 跑 RCA Worker 全測試與 static checks**

  Run: `cd rca-worker && uv run pytest tests -v && uv run ruff check . && uv run pyright`

  Expected: 全部 PASS。

- [ ] **Step 4: 跑 repository contracts**

  Run: `uv run --project backend pytest contracts/compatibility-tests -v`

  Expected: 全部 PASS。

- [ ] **Step 5: 跑端到端核心路徑**

  使用隔離 env files 啟動 Backend 與 RCA Worker，送入一筆新 Grafana webhook，驗證：HTTP 202、DB commit、只發布新 event、Worker claim、RCA terminal result。再讓 publisher 暫時失敗，驗證 event=`FAILED`、Backend 不自動重送；恢復 emulator 後呼叫 `/retry-failed`，驗證 publish 與 Worker idempotency。重啟 Backend 後確認未呼叫 API 前 backlog 不變。

- [ ] **Step 6: 執行安全與範圍 scan**

  Run: `! rg "MODEL_NAME|METRICS_MCP_URL|TRACE_MCP_URL|LOG_MCP_URL|MCP_CAPABILITY_MANIFEST|RCA_DEADLINE_SECONDS" backend/src deploy/k8s/base/backend-deployment.yaml .env.backend-api.example`

  Run: `! rg "outbox-deployment|partition-cronjob|sre-agent-outbox-worker|sre-agent-ensure-partitions" deploy backend/pyproject.toml`

  Run: `git diff --check && git status --short`

  Expected: scans exit 0；無 whitespace error；只有預期修改。

- [ ] **Step 7: Review migration acceptance evidence**

  保存但不 commit 含敏感資訊的操作輸出。人工確認 source/target row counts、FK validation、Pub/Sub IAM、maintenance rollback decision、legacy table storage。未完成確認不得排程 legacy cleanup。
