# 跨雲 Grafana Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 Grafana 傳入的 GCP／AWS alerts 以統一 labels 驗證、以穩定跨雲 identity 建立 Incident，並在資料異常或併發重送時安全地保存、隔離與去重。

**Architecture:** 在 normalization 與 persistence 之間新增純領域 `CrossCloudAlertValidator` 與 canonical Incident identity。PostgreSQL 保存每筆 alert 的 validation 結果，並以 partial unique index 保護 active Incident identity；ingestion 在同一 transaction 中先保存 delivery/event，再只對通過驗證且可解析 scope 的 alerts 建立 Incident/RCA/job/outbox。

**Tech Stack:** Python 3.12、Pydantic v2、SQLAlchemy 2 async、asyncpg、Alembic、PostgreSQL 18、pytest、Grafana webhook v1。

## Global Constraints

- 每筆 alert 必須具備 `alertname`、`cloud_provider`、`cloud_scope_id`、`resource_type`、`resource_id`、`environment`、`service`、`team`、`severity`、`signal_type`。
- `cloud_provider` 只接受 `gcp` 或 `aws`。
- `severity` 只接受 `critical`、`warning`、`info`；`signal_type` 只接受 `metric`、`log`、`trace`、`synthetic`。
- Incident identity 精確由 `source_id + cloud_provider + cloud_scope_id + resource_type + resource_id + alertname` 的 canonical SHA-256 產生。
- Grafana `groupKey` 原樣保存但不作為 Incident identity；同一 webhook 中 identity 不同的 alerts 不得強制合併。
- 通過驗證且 scope 可解析的 alert 直接建立 `QUEUED` RCA run、唯一 worker job 與唯一 outbox。
- 驗證缺漏或 scope 無法解析時保存 delivery 與 alert event，標記 `VALIDATION_FAILED`，但不建立 Incident、RCA、job、outbox，也不查詢 MCP。
- `resolved` 不得自動修改 human Incident status。
- 所有 accepted raw payload 永久保存在 PostgreSQL 18。
- 不加入 Cloud SQL、Kubernetes 或正式環境 infrastructure。

---

## File map

- Create: `backend/src/sre_agent/domain/alerts/cross_cloud.py` — 必要 labels 驗證、validation result、canonical Incident identity。
- Create: `backend/tests/unit/domain/alerts/test_cross_cloud.py` — 純領域 validation／identity tests。
- Modify: `backend/migrations/versions/0001_alert_incident_schema.py` — validation columns、Incident identity、active unique index、job uniqueness。
- Modify: `backend/src/sre_agent/persistence/repositories/alerts.py` — 保存 alert validation metadata。
- Modify: `backend/src/sre_agent/persistence/repositories/incidents.py` — identity-based create/reuse/reopen。
- Modify: `backend/src/sre_agent/persistence/repositories/jobs.py` — active-run conflict 不重建 job，job unique insertion。
- Modify: `backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py` — validation gate 與 identity orchestration。
- Modify: `backend/tests/integration/persistence/test_schema.py` — 新 schema invariants。
- Modify: `backend/tests/integration/application/test_ingest_grafana_alerts.py` — 跨雲、validation、identity、併發與 rollback。
- Modify: `docs/database/postgresql-schema.md` — migration DDL 同步。
- Modify: `contracts/examples/grafana-firing.json` — 符合跨雲必要 labels 的 GCP fixture。
- Create: `contracts/examples/grafana-firing-aws.json` — AWS fixture。
- Modify: `contracts/compatibility-tests/test_contracts.py` — 兩個跨雲 fixtures 通過 envelope contract。

### Task 1: 跨雲 labels 驗證與 Incident identity

**Files:**
- Create: `backend/src/sre_agent/domain/alerts/cross_cloud.py`
- Create: `backend/tests/unit/domain/alerts/test_cross_cloud.py`
- Modify: `contracts/examples/grafana-firing.json`
- Create: `contracts/examples/grafana-firing-aws.json`
- Modify: `contracts/compatibility-tests/test_contracts.py`

**Interfaces:**
- Produces: `CrossCloudAlertValidator.validate(labels: Mapping[str, str]) -> CrossCloudValidationResult`。
- Produces: `make_incident_identity(source_id: UUID, labels: Mapping[str, str]) -> str`。
- `CrossCloudValidationResult` contains `is_valid: bool` and ordered `errors: tuple[AlertValidationError, ...]`。
- `AlertValidationError` contains `field: str` and stable `code: str`; it must not contain secrets or raw payloads。

- [ ] **Step 1: 先寫 validation 與 identity tests**

測試必須使用手寫 literal expectations，覆蓋：

- 完整 GCP labels 與完整 AWS labels 通過。
- 每個必要欄位缺少、空白時回傳對應 `required` error。
- 非法 `cloud_provider`、`severity`、`signal_type` 回傳 `invalid_value`。
- AWS `cloud_scope_id` 必須為 12 位數字。
- GCP `cloud_scope_id` 必須符合 6–30 字元 project ID：小寫英文字母起始，只含小寫字母、數字、連字號，並以字母或數字結束。
- identity 對 labels 輸入順序不敏感。
- identity 因 source、provider、scope、resource type、resource ID 或 alertname 任一改變而不同。
- annotations、groupKey、severity、team、service 變更不改變 identity。
- validation 失敗時 `make_incident_identity` 拒絕執行，不產生猜測 identity。

- [ ] **Step 2: 執行並確認 RED**

```bash
cd backend
UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/domain/alerts/test_cross_cloud.py -v
```

Expected: collection FAIL，因為 `cross_cloud` module 尚不存在。

- [ ] **Step 3: 實作最小純領域模型**

使用 frozen dataclasses 與下列常數：

```python
REQUIRED_LABELS = (
    "alertname", "cloud_provider", "cloud_scope_id", "resource_type",
    "resource_id", "environment", "service", "team", "severity",
    "signal_type",
)
ALLOWED_CLOUD_PROVIDERS = frozenset({"gcp", "aws"})
ALLOWED_SEVERITIES = frozenset({"critical", "warning", "info"})
ALLOWED_SIGNAL_TYPES = frozenset({"metric", "log", "trace", "synthetic"})
INCIDENT_IDENTITY_FIELDS = (
    "cloud_provider", "cloud_scope_id", "resource_type", "resource_id",
    "alertname",
)
```

Identity canonical JSON 使用固定 key、`sort_keys=True`、compact separators 與 UTF-8，再計算 SHA-256 lowercase hex。

- [ ] **Step 4: 更新並驗證 GCP／AWS contract fixtures**

GCP fixture 使用已核准規格中的 GKE Pod 範例；AWS fixture 使用 RDS CPU 範例。兩者保持 Grafana v1 envelope，不增加自訂頂層 payload 格式。Compatibility test 載入兩個 fixtures 並用現有 OpenAPI format checker 驗證。

- [ ] **Step 5: GREEN、品質檢查與提交**

```bash
cd backend
UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/domain/alerts/test_cross_cloud.py ../contracts/compatibility-tests -v
UV_CACHE_DIR=$PWD/.uv-cache uv run ruff check src/sre_agent/domain/alerts/cross_cloud.py tests/unit/domain/alerts/test_cross_cloud.py
UV_CACHE_DIR=$PWD/.uv-cache uv run pyright src/sre_agent/domain/alerts/cross_cloud.py
git add backend/src/sre_agent/domain/alerts/cross_cloud.py backend/tests/unit/domain/alerts/test_cross_cloud.py contracts/examples/grafana-firing.json contracts/examples/grafana-firing-aws.json contracts/compatibility-tests/test_contracts.py
git diff --cached --check
git commit -m "feat: define cross-cloud alert identity"
```

### Task 2: PostgreSQL validation 與 identity invariants

**Files:**
- Modify: `backend/migrations/versions/0001_alert_incident_schema.py`
- Modify: `backend/tests/integration/persistence/test_schema.py`
- Modify: `backend/tests/unit/persistence/test_schema_documentation.py`
- Modify: `docs/database/postgresql-schema.md`

**Interfaces:**
- `alert_events.validation_status`: `VALID` 或 `VALIDATION_FAILED`。
- `alert_events.validation_errors`: JSONB array，default `[]`。
- `incidents.identity_key`: canonical identity SHA-256。
- Unique active Incident invariant: one `OPEN`／`INVESTIGATING` Incident per `identity_key`。
- Unique RCA job invariant: one `(rca_run_id, job_type)`。

- [ ] **Step 1: 先新增真 PostgreSQL schema assertions**

測試 catalog 必須斷言：

- `alert_events.validation_status TEXT NOT NULL DEFAULT 'VALID'`，CHECK 只允許 `VALID`、`VALIDATION_FAILED`。
- `alert_events.validation_errors JSONB NOT NULL DEFAULT '[]'::jsonb`。
- `incidents.identity_key TEXT NOT NULL`。
- `uq_incidents_active_identity` 為 `incidents(identity_key)` partial unique index，predicate 精確涵蓋 `OPEN`、`INVESTIGATING`。
- `uq_worker_jobs_run_type` 為 `worker_jobs(rca_run_id, job_type)` unique index。

- [ ] **Step 2: 執行 migration round-trip 並確認 RED**

```bash
cd backend
UV_CACHE_DIR=$PWD/.uv-cache uv run alembic downgrade base
UV_CACHE_DIR=$PWD/.uv-cache uv run alembic upgrade head
UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/integration/persistence/test_schema.py -v
```

Expected: 新 assertions FAIL，因為 columns/indexes 尚不存在。

- [ ] **Step 3: 修改未發布的 initial revision**

更新 `0001_alert_incident_schema.py`：

```sql
-- alert_events
validation_status TEXT NOT NULL DEFAULT 'VALID'
  CHECK (validation_status IN ('VALID', 'VALIDATION_FAILED')),
validation_errors JSONB NOT NULL DEFAULT '[]'::jsonb,

-- incidents
identity_key TEXT NOT NULL,

CREATE UNIQUE INDEX uq_incidents_active_identity
ON incidents (identity_key)
WHERE status IN ('OPEN', 'INVESTIGATING');

CREATE UNIQUE INDEX uq_worker_jobs_run_type
ON worker_jobs (rca_run_id, job_type);
```

因 branch 尚未發布，修改 `0001`；不得新增假裝已部署的第二 migration。

- [ ] **Step 4: 同步繁體中文 schema 文件**

更新完整 DDL、index 說明、validation failure 保留策略與 Incident identity concurrency 說明。Documentation coverage test 加入兩個新 index 名稱與關鍵 columns guard，但不以脆弱全文 equality 取代 catalog test。

- [ ] **Step 5: migration GREEN 與提交**

```bash
cd backend
UV_CACHE_DIR=$PWD/.uv-cache uv run alembic downgrade base
UV_CACHE_DIR=$PWD/.uv-cache uv run alembic upgrade head
UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/integration/persistence/test_schema.py tests/unit/persistence/test_schema_documentation.py -v
UV_CACHE_DIR=$PWD/.uv-cache uv run ruff check migrations tests/integration/persistence tests/unit/persistence
UV_CACHE_DIR=$PWD/.uv-cache uv run pyright migrations tests/integration/persistence tests/unit/persistence
git add backend/migrations/versions/0001_alert_incident_schema.py backend/tests/integration/persistence/test_schema.py backend/tests/unit/persistence/test_schema_documentation.py docs/database/postgresql-schema.md
git diff --cached --check
git commit -m "feat: enforce alert and incident identities"
```

### Task 3: Identity-based transactional ingestion 與併發

**Files:**
- Modify: `backend/src/sre_agent/persistence/repositories/alerts.py`
- Modify: `backend/src/sre_agent/persistence/repositories/incidents.py`
- Modify: `backend/src/sre_agent/persistence/repositories/jobs.py`
- Modify: `backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py`
- Modify: `backend/tests/integration/application/test_ingest_grafana_alerts.py`

**Interfaces:**
- `AlertRepository.add_event(..., validation_status: str, validation_errors: Sequence[AlertValidationError])` persists validation metadata。
- `IncidentRepository.get_or_create_active(identity_key, scope, ...) -> IncidentSelection` returns ID plus `created: bool` under concurrency。
- `IncidentRepository.latest_resolved(identity_key) -> UUID | None` only returns matching identity。
- `JobRepository.create_rca_work(...)` creates artifacts only when it inserts a new active run。

- [ ] **Step 1: 先改寫整合測試以符合跨雲規格**

所有正常 fixtures 加入完整 GCP／AWS labels。新增真 PostgreSQL tests：

- 相同 identity 的 firing update 沿用 active Incident。
- 同 resource、不同 `alertname` 建立兩個 Incident／RCA/job/outbox。
- 同 project/account、不同 `resource_id` 建立不同 Incident。
- 同 webhook 含兩個不同 identity alerts 時建立兩個 Incidents。
- 相同 identity 的 Incident 人工 resolved 後，新 firing 建立新 Incident，且 `reopened_from_incident_id` 精確指向該 identity 的前一筆。
- 缺少每個必要 label、空白、invalid enum、無法解析 internal scope 時：保存 delivery/event validation errors，沒有 Incident/RCA/job/outbox。
- GCP 與 AWS 正常告警都建立 `QUEUED` RCA work。
- resolved 依 identity 找到 Incident，但不修改 human status。
- 使用兩個獨立 sessions 與 test barrier 同時 ingest 相同 identity，斷言只建立一個 active Incident、run、job、outbox；兩筆 deliveries 都保留，dedup transition 只執行一次。
- 直接併發呼叫同 Incident 的 `create_rca_work`，斷言一個 run／job／outbox。

- [ ] **Step 2: 執行並確認 RED**

```bash
cd backend
UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/integration/application/test_ingest_grafana_alerts.py -v
```

Expected: identity isolation、validation failure、schema signature 與 concurrency tests FAIL。

- [ ] **Step 3: 實作 validation gate**

每筆 canonical event：

1. 執行 `CrossCloudAlertValidator`。
2. 驗證 labels 後執行既有 scope resolver/classifier；若結果非 `CLASSIFIED`，加入 stable `unknown_scope` errors。
3. 不論結果都保存 `alert_events` 與 raw JSON。
4. Validation failed 時不 upsert active alert instance、不建立 Incident 或 RCA artifacts。
5. Delivery 有任何 invalid alerts 時狀態設 `VALIDATION_FAILED`；若其餘 valid alerts 存在，仍正常處理那些 valid alerts。

Severity mapping 固定：

```text
critical -> SEV1
warning  -> SEV3
info     -> SEV4
```

- [ ] **Step 4: 實作 identity-based Incident repository**

- 移除 scope-only candidate selection。
- Active lookup、resolved lookup、create 都使用 `identity_key`。
- 建立 active Incident 使用 partial unique index conflict-safe insertion；若另一 transaction 已建立，取得同 identity active Incident 並 `FOR UPDATE`。
- `source_id + fingerprint` 僅用於 alert delivery/event dedup，不作 Incident candidate 條件。
- 每筆 alert 各自計算 identity；同 webhook 不保留隱含共享 Incident 變數。

- [ ] **Step 5: 修正 RCA/job idempotency**

`create_rca_work` 必須辨識 `rca_runs INSERT ... RETURNING` 是否真的建立新 run。若 conflict 後取得既有 active run，直接回傳，不能再建立 job。新 run 的 job 使用 `(rca_run_id, job_type)` conflict-safe insertion，outbox 沿用 `rca-run:{run_id}`。

- [ ] **Step 6: 證明 concurrency 與 rollback**

併發測試必須使用不同 `AsyncSession`／transaction，不能用 sequential calls 冒充。既有 injected failure rollback test 加入 validation/identity columns 後仍斷言 delivery、event、instance、Incident、run、job、outbox 全不存在。

- [ ] **Step 7: 完整 GREEN 與提交**

```bash
docker compose up -d postgres
cd backend
UV_CACHE_DIR=$PWD/.uv-cache uv run alembic downgrade base
UV_CACHE_DIR=$PWD/.uv-cache uv run alembic upgrade head
UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit tests/integration tests/contract -v
UV_CACHE_DIR=$PWD/.uv-cache uv run ruff check .
UV_CACHE_DIR=$PWD/.uv-cache uv run pyright
git add backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py backend/src/sre_agent/persistence/repositories backend/tests/integration/application/test_ingest_grafana_alerts.py
git diff --cached --check
git commit -m "fix: isolate cross-cloud alert incidents"
```

## Final verification

在三個 Tasks 都通過獨立 review 後執行：

```bash
cd backend
UV_CACHE_DIR=$PWD/.uv-cache uv run pytest -v
UV_CACHE_DIR=$PWD/.uv-cache uv run ruff check .
UV_CACHE_DIR=$PWD/.uv-cache uv run pyright
cd ..
UV_CACHE_DIR=backend/.uv-cache uv run --project backend pytest contracts/compatibility-tests -v
git diff --check
```

最終 whole-plan review 必須重新檢查：validation failure 永不建立 RCA work、identity 不會 scope-only 誤合併、concurrency 下 artifacts 唯一、文件與 migration 一致。
