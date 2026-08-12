# PostgreSQL Schema Reference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立一份繁體中文 PostgreSQL 18 schema 參考文件，完整呈現目前 Alembic migration 的資料表、約束、索引與月分割 SQL。

**Architecture:** Alembic migration 維持唯一可執行的 schema 來源；`docs/database/postgresql-schema.md` 提供經整理的閱讀版 SQL 與維運查詢。輕量一致性測試只比較 migration 與文件公開宣告的資料表、必要索引及分割表集合，真實資料庫行為仍由 PostgreSQL catalog 整合測試驗證。

**Tech Stack:** Markdown、PostgreSQL 18 SQL、Alembic、pytest。

## Global Constraints

- 文件使用繁體中文。
- Alembic migration 是建立與修改 schema 的唯一執行來源。
- 不建立可獨立部署的 `schema.sql`。
- 文件不得包含正式密碼、secret、Cloud SQL 建置、Kubernetes 或正式環境 infrastructure 配置。
- 文件中的資料表、約束、索引與分割規則必須與 `0001_alert_incident_schema.py` 一致。
- 六張月分割表使用 `PARTITION BY RANGE (partition_timestamp)` 與 `(id, partition_timestamp)` 複合主鍵。
- 每月 partition 的上界為下一個月一日 00:00 UTC，且為 exclusive upper bound。

---

## File map

- Create: `docs/database/postgresql-schema.md` — 繁體中文 schema、DDL、partition 與故障排查參考。
- Create: `backend/tests/unit/persistence/test_schema_documentation.py` — migration 與文件的涵蓋集合一致性檢查。
- Read only: `backend/migrations/versions/0001_alert_incident_schema.py` — 唯一 DDL 執行來源。
- Read only: `backend/src/sre_agent/persistence/database.py` — `ensure_monthly_partitions` 的實際行為來源。

### Task 1: 建立並驗證 PostgreSQL schema 參考文件

**Files:**
- Create: `docs/database/postgresql-schema.md`
- Create: `backend/tests/unit/persistence/test_schema_documentation.py`

**Interfaces:**
- Consumes: `DDL` 與 `PARTITIONED_TABLES` from `backend/migrations/versions/0001_alert_incident_schema.py`。
- Produces: 一份不具部署權威、但涵蓋目前 schema 的繁體中文 SQL 參考文件。

- [ ] **Step 1: 先寫文件一致性測試**

建立 `backend/tests/unit/persistence/test_schema_documentation.py`。使用 `importlib.util.spec_from_file_location` 載入 revision，從 `DDL` 以 `^CREATE TABLE ([a-z_]+)` 擷取 28 張父表，並從文件 fenced SQL block 擷取 `CREATE TABLE` 名稱。測試以固定集合排除 `_YYYY_MM` 範例 partition，斷言 migration 的父表集合完全包含於文件；另斷言六張 `PARTITIONED_TABLES` 都在文件中出現 `PARTITION BY RANGE (partition_timestamp)`，並斷言必要索引名稱：

```python
REQUIRED_INDEXES = {
    "uq_rca_runs_active_incident",
    "ix_webhook_deliveries_source_received",
    "ix_alert_events_source_fingerprint_observed",
    "ix_alert_instances_state_last_seen",
    "ix_incidents_scope_status_opened",
    "ix_evidence_records_run_observed",
    "ix_incident_messages_incident_created",
    "ix_incident_timeline_incident_occurred",
    "ix_audit_events_resource_occurred",
    "ix_worker_jobs_status_available",
    "ix_outbox_events_status_available",
}
```

測試只驗證公開 schema 覆蓋集合，不逐字比對整份 migration，也不取代真實 PostgreSQL catalog 測試。

- [ ] **Step 2: 執行測試並確認 RED**

Run:

```bash
cd backend
UV_CACHE_DIR=.uv-cache uv run pytest tests/unit/persistence/test_schema_documentation.py -v
```

Expected: FAIL，因為 `docs/database/postgresql-schema.md` 尚不存在。

- [ ] **Step 3: 撰寫參考文件**

建立 `docs/database/postgresql-schema.md`，依下列順序整理：

1. 「適用範圍與權威來源」：明示 Alembic 是唯一執行來源，文件 SQL 不可直接當 migration。
2. 「本機啟動與 migration」：列出 `docker compose up -d postgres`、health check、`uv run alembic upgrade head` 與 downgrade 指令；只使用本機範例帳密。
3. 「共同規則」：UUID、UTC `TIMESTAMPTZ`、JSONB、永久保留 accepted raw payload。
4. 「關係總覽」：用文字／Mermaid 說明 scope、Grafana delivery、alert、Incident、RCA、evidence、job/outbox。
5. 「非分割資料表」與「月分割資料表」：逐組放入 migration 目前使用的完整 `CREATE TABLE` SQL，包括所有 PK、FK、CHECK、UNIQUE。
6. 「索引」：放入 11 個具名索引 SQL，解釋 active RCA partial unique index 的併發用途。
7. 「Partition 維護」：列出六張 allowlist、current／next month 行為，以及 `2031_12` 到 `2032_01` exclusive bound 範例：

```sql
CREATE TABLE webhook_deliveries_2031_12
PARTITION OF webhook_deliveries
FOR VALUES FROM ('2031-12-01 00:00:00+00')
TO ('2032-01-01 00:00:00+00');
```

8. 「驗證與故障排查」：提供查詢 `pg_partitioned_table`、`pg_inherits`、`pg_indexes` 與 `information_schema.columns` 的唯讀 SQL。

- [ ] **Step 4: 執行 focused GREEN 與 mutation check**

Run:

```bash
cd backend
UV_CACHE_DIR=.uv-cache uv run pytest tests/unit/persistence/test_schema_documentation.py -v
```

Expected: PASS。

Mutation check：在未提交狀態暫時從文件移除 `worker_attempts` 的 `CREATE TABLE` 標題／SQL，確認測試失敗且指出缺少 `worker_attempts`；還原後重跑至 PASS。

- [ ] **Step 5: 執行 Task 1 完整驗證**

Run:

```bash
cd backend
UV_CACHE_DIR=.uv-cache uv run alembic downgrade base
UV_CACHE_DIR=.uv-cache uv run alembic upgrade head
UV_CACHE_DIR=.uv-cache uv run pytest tests/integration/persistence/test_schema.py tests/unit/persistence/test_schema_documentation.py -v
UV_CACHE_DIR=.uv-cache uv run ruff check tests/unit/persistence/test_schema_documentation.py
UV_CACHE_DIR=.uv-cache uv run pyright tests/unit/persistence/test_schema_documentation.py
```

Expected: migration round-trip PASS、所有 focused tests PASS、Ruff/Pyright clean。

- [ ] **Step 6: 自我審查並提交**

確認文件沒有未完成標記、沒有正式 secret、沒有 Cloud/Kubernetes/prod infrastructure，且所有 SQL 名稱與 migration 相同。

```bash
git add docs/database/postgresql-schema.md backend/tests/unit/persistence/test_schema_documentation.py
git diff --cached --check
git commit -m "docs: add PostgreSQL schema reference"
```
