# Immutable Migration Rollout Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the pre-rollout ordinary-table migration, add its Worker validation gate, restore Worker evidence fidelity without reintroducing composite references, then complete the remaining runtime, deployment, documentation, and release gates.

**Architecture:** The migration contract is a four-gate maintenance-window sequence: Backend `0002_grafana_normalization_v2`, Worker `0002_adk_specialist_analysis`, corrected Backend `0003_non_partition_runtime_tables`, then Worker `0003_validate_ordinary_runtime_tables`. Backend `0003` copies the real Worker `0002` source schema into ordinary UUID-keyed tables; Worker `0003` validates that conversion and installs only post-conversion UUID constraints. Runtime changes wait until both version tables and both schema checks are complete.

**Tech Stack:** PostgreSQL 16, Alembic, SQLAlchemy async, asyncpg, FastAPI, Pydantic v2, pytest, Ruff, Pyright, Docker Compose, Kustomize.

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
- `0001_rca_worker_v1` 與 `0002_adk_specialist_analysis` 已發布且永久不可修改、squash、stamp 或重放。
- `0003_non_partition_runtime_tables` 是尚未獲 staging/production rollout acceptance 的 release-candidate revision：既有 disposable test execution 不構成發布；Task 1 是唯一允許在 staging/production 執行前原地修正它的工作。Task 1 commit 後不得再改該 revision，之後的缺陷必須新增 forward revision。
- 每個 rollout gate 使用明確 revision；不得使用 `alembic upgrade head` 跨越未驗證 gate，亦不得在 migration environment 以 schema 分支或 stamp 偽造完成狀態。
- 在 Backend `0003` 與 Worker `0003_validate_ordinary_runtime_tables` 都成功且 postcondition catalog checks 皆通過前，停止 Backend 與 Worker writes，且不得啟動 UUID-only runtime。

## Prerequisites and File Map

**Required commits already present:** `b400b88` (initial Backend 0003), `7925554` (migration test isolation), `d3d393b` (Backend UUID-only SQL), `5622169` (bounded disposable DB helper), `60229b5` (UUID-only Worker references), and `db246a4` (immutable four-gate spec).

`60229b5` is retained: `EvidenceReference(id: UUID)`, report `evidenceId`, UUID-only joins, and the removal of partition helper fields remain required. Task 3 changes only the mistaken loss of exact evidence bytes/provenance and `result_status` persistence.

| Path | Responsibility after this plan |
| --- | --- |
| `backend/migrations/versions/0003_non_partition_runtime_tables.py` | Correct pre-rollout conversion from true Worker 0002 source schema. |
| `backend/tests/integration/persistence/test_four_stage_migration.py` | Real clean and existing Worker-head four-gate acceptance. |
| `rca-worker/migrations/versions/0003_validate_ordinary_runtime_tables.py` | Worker-owned post-conversion validation and UUID-only constraint/index gate. |
| `rca-worker/tests/integration/persistence/test_four_stage_conversion.py` | Worker 0003 wrong-gate, no-op, and catalog acceptance. |
| `rca-worker/src/sre_rca_worker/persistence/repositories/rca.py` | Exact raw bytes, content hash, and provenance persistence with UUID-only lookup. |
| `rca-worker/src/sre_rca_worker/application/rca/processor.py` | Persist canonical `rca_reports.result_status`. |
| `backend/src/sre_agent/workers/` and `backend/src/sre_agent/config/outbox_settings.py` | Removed obsolete polling and partition runtimes only after migration proof. |
| `docker-compose.yml` and `deploy/k8s/` | Runtime ownership after obsolete workloads disappear. |
| `README.md`, component READMEs, `docs/database/postgresql-schema.md` | Exact four-revision operator procedure and final architecture. |

---

### Task 1: Correct Backend 0003 from the real Worker 0002 source

**Files:**

- Modify: `backend/migrations/versions/0003_non_partition_runtime_tables.py`
- Create: `backend/tests/integration/persistence/test_four_stage_migration.py`
- Modify: `backend/tests/integration/persistence/test_non_partition_migration.py`
- Modify: `backend/tests/unit/persistence/test_schema_documentation.py`

**Interfaces:**

- Consumes: Worker `0001_rca_worker_v1.upgrade()` exact evidence/lifecycle contract and Worker `0002_adk_specialist_analysis.upgrade()` exact specialist-analysis contract.
- Produces: corrected `upgrade() -> None` with `evidence_records(raw_result BYTEA NOT NULL, metadata JSONB NOT NULL, content_hash TEXT NOT NULL)` and no `raw_result_reference`; canonical tables retain UUID-only references.
- Produces: `async def upgrade_four_gates(database_url: str, *, existing_worker_head: bool) -> None` test helper that invokes exact revisions only.

**Reviewer gate:** Reject unless the replacement DDL and copy SQL preserve exact bytes and JSON metadata, the existing `rca_reports.result_status` and Worker lifecycle/analysis catalog objects survive unchanged, and every wrong source version fails before any replacement table is created.

- [ ] **Step 1: Write failing source-contract tests (2–5 minutes)**

  In `test_four_stage_migration.py`, create a clean database, execute Backend `0002_grafana_normalization_v2` then Worker `0002_adk_specialist_analysis`, and seed two partition months with `raw_result=b"\x00\xffnon-utf8\n{\"a\":1.00}"`, `metadata={"contentType":"application/json","source":"metrics"}`, a SHA-256 content hash, `rca_reports.result_status='PARTIAL'`, lifecycle `failure_code='MCP_TIMEOUT'`, and a `specialist_runs.analysis_result` object. Assert the schema source contains the real Worker fields before conversion.

  ```python
  assert bytes(before["raw_result"]) == b"\x00\xffnon-utf8\n{\"a\":1.00}"
  assert before["metadata"] == {"contentType": "application/json", "source": "metrics"}
  assert await connection.fetchval(
      "SELECT result_status FROM rca_reports WHERE rca_run_id=$1", run_id
  ) == "PARTIAL"
  ```

- [ ] **Step 2: Run the focused RED case (2–5 minutes)**

  Run: `cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent uv run pytest tests/integration/persistence/test_four_stage_migration.py::test_backend_0003_preserves_worker_0002_evidence_and_lifecycle -v`

  Expected: FAIL while running Backend `0003` because its current copy selects `raw_result_reference`, which Worker `0001` removed; the error names `raw_result_reference` or the post-upgrade exact-byte assertion fails. The database fixture must be dropped in `finally` even on this expected failure.

- [ ] **Step 3: Make Backend 0003 validate the exact source and copy it verbatim (2–5 minutes)**

  Add a preflight invoked before `_assert_no_duplicate_uuids()` that reads both version tables and catalogs. It must require exactly Backend `0002_grafana_normalization_v2`, Worker `0002_adk_specialist_analysis`, and these Worker-owned conditions: evidence `raw_result`, `metadata`, `content_hash`; report `result_status` with its three-value check; lifecycle `failure_code` columns; `worker_jobs` lease/attempt fields; and specialist analysis columns/checks.

  ```python
  def _require_worker_0002_source() -> None:
      connection = op.get_bind()
      worker_revision = connection.exec_driver_sql(
          "SELECT version_num FROM alembic_version_rca_worker"
      ).scalar_one_or_none()
      if worker_revision != "0002_adk_specialist_analysis":
          raise RuntimeError("Worker 0002_adk_specialist_analysis is required")
      required = {"raw_result", "metadata", "content_hash"}
      actual = _column_names(connection, "evidence_records")
      if not required <= actual or "raw_result_reference" in actual:
          raise RuntimeError("evidence_records is not the Worker 0002 source schema")
  ```

  Replace the evidence fragment of `REPLACEMENT_DDL` and `COPY_STATEMENTS` with this exact semantic shape; retain all existing business checks and scope foreign keys.

  ```sql
  raw_result BYTEA NOT NULL,
  metadata JSONB NOT NULL,
  content_hash TEXT NOT NULL,
  CONSTRAINT ck_evidence_records_metadata_object
      CHECK (jsonb_typeof(metadata) = 'object')

  INSERT INTO evidence_records_new (
      id, observed_at, rca_run_id, specialist_run_id, evidence_type,
      source_agent, source_endpoint, tool_name, team_id, project_id,
      environment_id, service_id, time_window_start, time_window_end,
      structured_data, raw_result, metadata, content_hash
  ) SELECT
      id, observed_at, rca_run_id, specialist_run_id, evidence_type,
      source_agent, source_endpoint, tool_name, team_id, project_id,
      environment_id, service_id, time_window_start, time_window_end,
      structured_data, raw_result, metadata, content_hash
  FROM evidence_records;
  ```

  Do not recreate `rca_reports`, `rca_runs`, `worker_jobs`, `worker_attempts`, or `specialist_runs`; catalog assertions prove their Worker `0002` columns and constraints remain untouched. Do not change Worker `0001` or `0002` files.

- [ ] **Step 4: Add fail-closed and preservation acceptance cases (2–5 minutes)**

  Add tests for: Worker version still `0001_rca_worker_v1`; a source with `metadata` dropped; a source with a missing `result_status` check; duplicate evidence UUIDs; and rerunning Backend `0003` after its matching version row. The first four must raise `RuntimeError` or duplicate-precheck SQL error before canonical names change. The matching rerun is the sole no-op case and must leave the catalog unchanged.

  ```python
  with pytest.raises(RuntimeError, match="Worker 0002_adk_specialist_analysis is required"):
      await asyncio.to_thread(upgrade_backend, database.url, "0003_non_partition_runtime_tables")
  assert await table_oid(connection, "evidence_records") == original_oid
  ```

- [ ] **Step 5: Run the focused GREEN tests (2–5 minutes)**

  Run: `cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent uv run pytest tests/integration/persistence/test_four_stage_migration.py tests/integration/persistence/test_non_partition_migration.py tests/unit/persistence/test_schema_documentation.py -v`

  Expected: PASS. The canonical six tables are ordinary relations with one-column UUID primary keys; retained legacy parents are partitioned; exact bytes, metadata, hashes, report status, failure codes, and analysis columns match the seeded Worker 0002 rows.

- [ ] **Step 6: Commit the pre-rollout correction (2–5 minutes)**

  ```bash
  git add backend/migrations/versions/0003_non_partition_runtime_tables.py backend/tests/integration/persistence/test_four_stage_migration.py backend/tests/integration/persistence/test_non_partition_migration.py backend/tests/unit/persistence/test_schema_documentation.py
  git commit -m "fix: preserve worker evidence in backend migration"
  ```

### Task 2: Add the Worker 0003 post-conversion validation gate

**Files:**

- Create: `rca-worker/migrations/versions/0003_validate_ordinary_runtime_tables.py`
- Create: `rca-worker/tests/integration/persistence/test_four_stage_conversion.py`
- Modify: `rca-worker/tests/integration/persistence/test_schema.py`
- Modify: `backend/tests/integration/persistence/test_four_stage_migration.py`

**Interfaces:**

- Consumes: Backend version `0003_non_partition_runtime_tables` and Worker version `0002_adk_specialist_analysis`.
- Produces: Worker Alembic revision `0003_validate_ordinary_runtime_tables`, `down_revision = "0002_adk_specialist_analysis"`, which validates but never replays Worker data migrations.
- Produces: `validate_post_conversion_schema(connection: Connection) -> None` used internally by the revision and called only in test catalog assertions through SQL, not imported by runtime.

**Reviewer gate:** Reject if the revision can run before corrected Backend 0003, rewrites raw evidence/lifecycle/analysis data, accepts a composite evidence FK, bypasses a wrong version with stamping, or does not test both clean and existing Worker-head databases.

- [ ] **Step 1: Write failing Worker-gate tests (2–5 minutes)**

  Add cases that call Worker target `0003_validate_ordinary_runtime_tables` against: Backend at `0002`; Worker at `0001`; Backend `0003` with a restored `evidence_partition_timestamp` column; and a correct post-conversion database. Assert the first three fail closed and the fourth succeeds without changing seeded data.

  ```python
  with pytest.raises(RuntimeError, match="Backend 0003_non_partition_runtime_tables is required"):
      await asyncio.to_thread(upgrade_worker, database.url, "0003_validate_ordinary_runtime_tables")
  ```

- [ ] **Step 2: Run the RED cases (2–5 minutes)**

  Run: `cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent uv run pytest tests/integration/persistence/test_four_stage_conversion.py -v`

  Expected: FAIL because revision `0003_validate_ordinary_runtime_tables` does not exist.

- [ ] **Step 3: Implement a validation-only revision (2–5 minutes)**

  Implement explicit version and catalog checks. No `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, copy loop, or replacement table appears in this file. It may create only missing ordinary-table indexes and UUID-only foreign-key/unique constraints proven absent from corrected Backend 0003.

  ```python
  revision = "0003_validate_ordinary_runtime_tables"
  down_revision = "0002_adk_specialist_analysis"

  def upgrade() -> None:
      connection = op.get_bind()
      _require_version(connection, "alembic_version_backend", "0003_non_partition_runtime_tables")
      _require_version(connection, "alembic_version_rca_worker", "0002_adk_specialist_analysis")
      _require_ordinary_uuid_primary_keys(connection, (
          "webhook_deliveries", "alert_events", "evidence_records",
          "incident_messages", "incident_timeline_events", "audit_events",
      ))
      _require_evidence_fidelity_columns(connection)
      _require_worker_0002_lifecycle_and_analysis(connection)
      _require_uuid_only_dependent_foreign_keys(connection)
  ```

  `downgrade()` must raise `RuntimeError("Worker 0003 is a forward validation gate; do not downgrade across the ordinary-table conversion")`.

- [ ] **Step 4: Add real clean and existing four-gate acceptance (2–5 minutes)**

  `test_four_stage_migration.py` executes these exact targets on a disposable clean database:

  ```text
  backend: 0002_grafana_normalization_v2
  worker:  0002_adk_specialist_analysis
  backend: 0003_non_partition_runtime_tables
  worker:  0003_validate_ordinary_runtime_tables
  ```

  `test_four_stage_conversion.py` seeds a database already at the first two targets, verifies its version rows and Worker catalog, then executes only the last two targets. Both cases assert identical final version rows, relation kinds, required Worker columns, UUID-only FKs, exact evidence values, and legacy partition parents. Neither case may use `stamp` or `upgrade head`.

- [ ] **Step 5: Run all migration acceptance evidence (2–5 minutes)**

  Run: `cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent uv run pytest tests/integration/persistence/test_four_stage_migration.py -v`

  Run: `cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent uv run pytest tests/integration/persistence/test_four_stage_conversion.py tests/integration/persistence/test_schema.py -v`

  Expected: PASS. Every disposable test database is closed and dropped with bounded cleanup; wrong gates leave no partial replacement tables.

- [ ] **Step 6: Commit the validation gate (2–5 minutes)**

  ```bash
  git add rca-worker/migrations/versions/0003_validate_ordinary_runtime_tables.py rca-worker/tests/integration/persistence/test_four_stage_conversion.py rca-worker/tests/integration/persistence/test_schema.py backend/tests/integration/persistence/test_four_stage_migration.py
  git commit -m "feat: validate worker ordinary-table migration gate"
  ```

### Task 3: Restore exact Worker evidence fidelity while retaining UUID-only references

**Files:**

- Modify: `rca-worker/src/sre_rca_worker/persistence/repositories/rca.py`
- Modify: `rca-worker/src/sre_rca_worker/application/rca/processor.py`
- Modify: `rca-worker/tests/integration/application/test_persist_evidence.py`
- Modify: `rca-worker/tests/integration/application/test_persist_report.py`
- Modify: `rca-worker/tests/integration/application/test_production_processor.py`
- Modify: `rca-worker/tests/eval/test_rca_reports.py`
- Modify: `rca-worker/tests/unit/application/test_processor_retry.py`

**Interfaces:**

- Consumes: `EvidenceReference(id: UUID)` from `domain/evidence/models.py`; this model remains unchanged.
- Produces: evidence inserts that persist `raw_result: bytes`, provenance `metadata: dict[str, Any]`, and `content_hash: str`. `PersistedEvidence` keeps its existing structured-data-only application shape so exact raw payloads are not pulled into agent context; list/get ownership queries remain UUID-only (`id`, `rca_run_id`, and `specialist_run_id`).
- Produces: report persistence that inserts `result_status: Literal["COMPLETE", "PARTIAL", "FAILED"]` computed from `RcaReportDraft` terminal state.

**Reviewer gate:** Reject if any partition helper returns, raw bytes are transformed or replaced by a pointer, provenance is omitted, report result status is not stored, UUID-only report/agent contracts change, or the disposable canonical smoke does not read back exact values.

- [ ] **Step 1: Write fidelity regressions against commit 60229b5 (2–5 minutes)**

  Replace the current pointer assertion with exact-byte and provenance assertions. Use a non-UTF-8 byte sequence and non-canonical JSON input; include a `metadata` object carrying content type, normalized scope, request window, and input hash. Add a report test with a partial report and assert `result_status='PARTIAL'`.

  ```python
  assert bytes(row["raw_result"]) == raw
  assert row["metadata"] == expected_metadata
  assert row["content_hash"] == hashlib.sha256(raw).hexdigest()
  assert report_row["result_status"] == "PARTIAL"
  assert reference.model_dump(mode="json") == {"id": str(reference.id)}
  ```

- [ ] **Step 2: Run focused RED tests (2–5 minutes)**

  Run: `cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent uv run pytest tests/integration/application/test_persist_evidence.py tests/integration/application/test_persist_report.py -v`

  Expected: FAIL because `60229b5` selects/writes `raw_result_reference` and its `INSERT INTO rca_reports` omits `result_status`.

- [ ] **Step 3: Restore canonical persistence fields, not composite keys (2–5 minutes)**

  Change evidence insert/list/get SQL to use only UUID ownership predicates and the real ordinary-table fields. Derive the metadata object from the immutable `EvidenceDraft` provenance rather than serializing a replacement pointer.

  ```python
  evidence_metadata = {
      "contentType": draft.content_type,
      "inputSha256": draft.input_sha256,
      "inputScope": draft.input_scope.model_dump(mode="json"),
      "normalizedScope": draft.normalized_scope.model_dump(mode="json"),
      "requestWindowStart": draft.request_window_start.isoformat(),
      "requestWindowEnd": draft.request_window_end.isoformat(),
  }
  ```

  ```sql
  INSERT INTO evidence_records (
      observed_at, rca_run_id, specialist_run_id, evidence_type, source_agent,
      source_endpoint, tool_name, time_window_start, time_window_end,
      structured_data, raw_result, metadata, content_hash
  ) VALUES (
      :observed_at, :rca_run_id, :specialist_run_id, :evidence_type, :source_agent,
      :source_endpoint, :tool_name, :window_start, :window_end,
      CAST(:structured_data AS JSONB), :raw_result, CAST(:metadata AS JSONB), :content_hash
  ) RETURNING id
  ```

  Keep `PersistedEvidence` and evidence-tool receipts unchanged: list/get continue selecting only the ownership fields, `structured_data`, and fields already required for chunking. Exact bytes and metadata are durability/audit fields verified directly in persistence tests, not content returned to the AI. In `_persist_report`, set `result_status=report.status` and add the column to an aggregate `INSERT` that also creates version 1 when no prior report exists.

  ```sql
  INSERT INTO rca_reports(rca_run_id, version, summary, report, result_status)
  SELECT :run,
         COALESCE((SELECT max(version) FROM rca_reports WHERE rca_run_id = :run), 0) + 1,
         :summary, CAST(:report AS JSONB), :result_status
  ```

- [ ] **Step 4: Prove audit, agent, and UUID-only behavior together (2–5 minutes)**

  Extend production processor tests to persist evidence, retrieve it through `get_specialist_evidence`, create `hypothesis_evidence`, and persist a report. Assert the only evidence reference payload remains `{"evidenceId": "UUID"}` and query predicates never contain partition helper columns.

  ```bash
  ! rg "partition_timestamp|partitionTimestamp|evidence_partition_timestamp|alert_event_partition_timestamp" rca-worker/src/sre_rca_worker
  ```

- [ ] **Step 5: Run focused GREEN plus disposable canonical smoke (2–5 minutes)**

  Run: `cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent uv run pytest tests/integration/application/test_persist_evidence.py tests/integration/application/test_persist_report.py tests/integration/application/test_production_processor.py tests/eval/test_rca_reports.py tests/unit/application/test_processor_retry.py -v`

  Run: `cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent uv run pytest tests/integration/persistence/test_four_stage_migration.py::test_clean_four_gate_database_runs_uuid_only_worker_smoke -v`

  Expected: PASS. The smoke starts from four explicit migration targets, persists exact bytes and metadata through Worker runtime, verifies `result_status`, and never invokes a migration `head` target.

- [ ] **Step 6: Commit the superseding runtime fix (2–5 minutes)**

  ```bash
  git add rca-worker/src/sre_rca_worker/persistence/repositories/rca.py rca-worker/src/sre_rca_worker/application/rca/processor.py rca-worker/tests/integration/application/test_persist_evidence.py rca-worker/tests/integration/application/test_persist_report.py rca-worker/tests/integration/application/test_production_processor.py rca-worker/tests/eval/test_rca_reports.py rca-worker/tests/unit/application/test_processor_retry.py
  git commit -m "fix: preserve exact uuid-only worker evidence"
  ```

### Task 4: Remove Outbox and Partition runtimes after migration proof

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
- Modify: `contracts/compatibility-tests/test_design_consistency.py`

**Interfaces:**

- Consumes: `OutboxPublishService.publish_event(event_id: UUID)` and recovery API from Tasks 4–6 of the original plan; these are retained.
- Produces: no standalone outbox polling entrypoint, no partition creation entrypoint, and no runtime imports of their settings/helpers.

**Reviewer gate:** Reject if deletion occurs before Tasks 1–3 evidence is attached, if it removes explicit event publication/recovery, or if a console script, import, polling loop, or partition creator remains.

- [ ] **Step 1: Add the failing removal contract (2–5 minutes)**

  Add exact assertions that `backend/pyproject.toml` contains neither `sre-agent-outbox-worker` nor `sre-agent-ensure-partitions`, and source has no `OutboxSettings`, `ensure_monthly_partitions`, `PARTITIONED_TABLES`, `outbox_main`, or `partition_worker` import.

- [ ] **Step 2: Run RED (2–5 minutes)**

  Run: `uv run --project backend pytest contracts/compatibility-tests/test_design_consistency.py -v`

  Expected: FAIL because obsolete entrypoints and modules still exist.

- [ ] **Step 3: Delete only obsolete runtime ownership (2–5 minutes)**

  Remove the listed files, console scripts, and their tests. Preserve `application/outbox/publish_events.py`, `application/outbox/recover_events.py`, `integrations/pubsub/publisher.py`, and their Backend composition lifecycle.

- [ ] **Step 4: Run GREEN and static scan (2–5 minutes)**

  Run: `uv run --project backend pytest contracts/compatibility-tests/test_design_consistency.py -v && ! rg "outbox_main|OutboxSettings|ensure_monthly_partitions|PARTITIONED_TABLES|sre-agent-outbox-worker|sre-agent-ensure-partitions" backend/src backend/pyproject.toml`

  Expected: PASS and the scan has no matches.

- [ ] **Step 5: Commit (2–5 minutes)**

  ```bash
  git add -A backend/src/sre_agent backend/tests backend/pyproject.toml contracts/compatibility-tests/test_design_consistency.py
  git commit -m "refactor: remove outbox and partition workers"
  ```

### Task 5: Simplify Compose and Kubernetes runtime ownership

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

**Interfaces:**

- Consumes: Backend publisher configuration from Task 5 original runtime composition; Worker subscriber/AI/MCP ownership; five isolated env-file contract.
- Produces: rendered manifests where Backend has publisher identity/config, Worker has subscriber AI/MCP config, and no removed workload references.

**Reviewer gate:** Reject if Backend receives Worker AI/MCP values, Worker receives Backend publisher privileges, manifests refer to deleted workloads, or Compose implicitly loads application env files.

- [ ] **Step 1: Add failing manifest ownership assertions (2–5 minutes)**

  Assert Backend deployment includes Pub/Sub project/topic and publisher identity binding but excludes `MODEL_NAME`, all MCP URLs, analysis mode, evidence limits, and `RCA_DEADLINE_SECONDS`. Assert Worker owns subscriber plus AI/MCP settings, `kustomization.yaml` excludes both deleted resources, and no outbox service account remains.

- [ ] **Step 2: Run RED (2–5 minutes)**

  Run: `uv run --project backend pytest contracts/compatibility-tests/test_gke_manifests.py -v`

  Expected: FAIL because the deleted workloads and old environment ownership are still rendered.

- [ ] **Step 3: Update exact runtime ownership (2–5 minutes)**

  Delete the two manifests and their kustomization/service-account references. Keep Backend `PUBSUB_PROJECT_ID`, `RCA_TOPIC_ID`, and workload identity publisher permission; keep Worker `PUBSUB_SUBSCRIPTION_ID`, `MODEL_NAME`, MCP fields, and subscriber permission. Set Compose interpolation only through `.env.compose` values:

  ```yaml
  ports:
    - "${POSTGRES_HOST_PORT:-5432}:5432"
    - "${PUBSUB_HOST_PORT:-58085}:8085"
  ```

  Documentation commands use `docker compose --env-file .env.compose`; Compose does not name an application `env_file`.

- [ ] **Step 4: Run GREEN and render checks (2–5 minutes)**

  Run: `uv run --project backend pytest contracts/compatibility-tests/test_gke_manifests.py -v && docker compose --env-file .env.compose.example config >/dev/null && kubectl kustomize deploy/k8s/base >/dev/null`

  Expected: PASS; render contains neither an Outbox Deployment nor a Partition CronJob.

- [ ] **Step 5: Commit (2–5 minutes)**

  ```bash
  git add -A docker-compose.yml deploy/k8s contracts/compatibility-tests/test_gke_manifests.py
  git commit -m "refactor: simplify runtime deployments"
  ```

### Task 6: Publish the immutable four-revision runbook and schema documentation

**Files:**

- Modify: `README.md`
- Modify: `backend/README.md`
- Modify: `rca-worker/README.md`
- Modify: `deploy/k8s/README.md`
- Modify: `docs/database/postgresql-schema.md`
- Modify: `contracts/compatibility-tests/test_design_consistency.py`

**Interfaces:**

- Consumes: exact Worker revision `0003_validate_ordinary_runtime_tables`; all four migration targets; Task 4 removed runtime names; Task 5 manifests.
- Produces: one unambiguous operator sequence and a schema reference that describes exact evidence bytes/provenance, `result_status`, lifecycle, specialist analysis, UUID-only references, and retained legacy tables.

**Reviewer gate:** Reject if any operator document uses `upgrade head`, implies automatic outbox replay, describes `raw_result_reference` as canonical evidence, omits either version table, or says a post-write downgrade is lossless.

- [ ] **Step 1: Add failing document-contract tests (2–5 minutes)**

  Add assertions for all five `.env.*.example` names, the three protected recovery paths, absence of removed runtime startup commands, and each exact migration command below. Assert the document contains `raw_result BYTEA`, `metadata JSONB`, `content_hash`, `result_status`, and retained `__partitioned_legacy_0003` tables.

- [ ] **Step 2: Run RED (2–5 minutes)**

  Run: `uv run --project backend pytest contracts/compatibility-tests/test_design_consistency.py -v`

  Expected: FAIL because the documents still name an abbreviated migration sequence or obsolete workers.

- [ ] **Step 3: Write the explicit maintenance-window commands (2–5 minutes)**

  Document these four commands in this order, with writes stopped before the first and runtimes still stopped until the fourth postcondition passes:

  ```bash
  (cd backend && BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration uv run alembic upgrade 0002_grafana_normalization_v2)
  (cd rca-worker && RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration uv run alembic upgrade 0002_adk_specialist_analysis)
  (cd backend && BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration uv run alembic upgrade 0003_non_partition_runtime_tables)
  (cd rca-worker && RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration uv run alembic upgrade 0003_validate_ordinary_runtime_tables)
  ```

  Include two read-only checks after each command:

  ```sql
  SELECT version_num FROM alembic_version_backend;
  SELECT version_num FROM alembic_version_rca_worker;
  ```

  State that existing Worker-head databases verify the first two revision rows/catalog and execute only commands three and four; they never stamp or replay migrations. State rollback policy: preserve legacy tables, stop writes on failure, and restore/migrate deltas from an approved backup after new writes; do not run Alembic downgrade.

- [ ] **Step 4: Run GREEN and stale-text scan (2–5 minutes)**

  Run: `uv run --project backend pytest contracts/compatibility-tests/test_design_consistency.py -v && ! rg "alembic upgrade head|raw_result_reference.*canonical|sre-agent-outbox-worker|sre-agent-ensure-partitions" README.md backend/README.md rca-worker/README.md deploy/k8s/README.md docs/database/postgresql-schema.md`

  Expected: PASS with no stale operational instruction.

- [ ] **Step 5: Commit (2–5 minutes)**

  ```bash
  git add README.md backend/README.md rca-worker/README.md deploy/k8s/README.md docs/database/postgresql-schema.md contracts/compatibility-tests/test_design_consistency.py
  git commit -m "docs: document immutable migration rollout"
  ```

### Task 7: Execute the full release gate without crossing migration gates

**Files:**

- Modify only when a failing verification identifies its owning task: the exact file set from Tasks 1–6.
- Create locally but do not commit: maintenance evidence under a secure operator-controlled directory outside the repository.

**Interfaces:**

- Consumes: commits from Tasks 1–6 and existing request-scoped publish/recovery implementation.
- Produces: release acceptance evidence for schema conversion, isolated configuration, Backend/Worker runtime behavior, deployment rendering, and static/type checks.

**Reviewer gate:** Reject release if any four-gate command is replaced with `head`, any full test/type failure is waived, the acceptance/test database names are not isolated from `sre_agent`, the canonical smoke skips exact evidence provenance, or retained legacy table verification is absent.

- [ ] **Step 1: Establish a fresh disposable acceptance database with explicit revisions (2–5 minutes)**

  Open and retain one Bash terminal named `Task 7 verification`; all task-specific variables below live in that terminal through Step 6. Discover running containers by their exact published host ports, not by an implicit Compose project. Zero or multiple candidates for either port is a hard stop. Inspect and validate the candidates against the repository's Postgres/Pub/Sub service signatures, record their exact names, bindings, and shared database OID, and never substitute a different container later. The Postgres validation deliberately accepts the inspected standalone project container without requiring Compose labels; the image, command, environment, writable data mount, binding, and database catalog together are its ownership proof. Pub/Sub must carry the expected emulator command and exact Compose service label. Its config-file label must equal either the current worktree's Compose file or the main-checkout Compose file derived from this worktree's absolute Git common directory; capture the accepted exact label and require that same value later. Do not accept a parent/prefix match, a symlink-resolved substitute, or any other path.

  ```bash
  # Run this block in Bash and keep this named terminal open through cleanup.
  set +e
  set -o pipefail
  task7_gate_failed='false'
  task7_cleanup_failed='false'
  task7_mutation_started='false'
  task7_preflight_complete='false'
  task7_postgres_container=''
  task7_postgres_id=''
  task7_postgres_binding=''
  task7_shared_oid_before=''
  task7_pubsub_container=''
  task7_pubsub_id=''
  task7_pubsub_binding=''
  task7_pubsub_compose_file=''
  task7_fail() {
    echo "$1" >&2
    task7_gate_failed='true'
    return 1
  }
  task7_require_open_gate() {
    if [ "$task7_gate_failed" != 'false' ]; then
      echo "refusing Task 7 phase $1 because an earlier phase failed; enter Step 6 cleanup" >&2
      return 1
    fi
  }
  task7_run_phase() {
    task7_phase_name=$1
    shift
    task7_require_open_gate "$task7_phase_name" || return 1
    "$@" || {
      if [ "$task7_gate_failed" = 'false' ]; then
        task7_fail "Task 7 phase failed: $task7_phase_name"
      fi
      return 1
    }
  }
  task7_shutdown_poll_attempts=15
  task7_shutdown_poll_seconds=1
  task7_exact_job_active() {
    local task7_job_pid=${1:-}
    [ -n "$task7_job_pid" ] || return 1
    kill -0 "$task7_job_pid" 2>/dev/null || return 1
    jobs -pr | grep -Fx "$task7_job_pid" >/dev/null && return 0
    jobs -ps | grep -Fx "$task7_job_pid" >/dev/null && return 0
    echo "PID $task7_job_pid is live but is not a retained Bash job; refusing to signal it" >&2
    return 2
  }
  task7_reap_exact_job() {
    local task7_reap_pid=$1
    local task7_reap_label=$2
    local task7_reap_status
    wait "$task7_reap_pid" 2>/dev/null
    task7_reap_status=$?
    printf '%s PID %s reaped with status %s\n' "$task7_reap_label" "$task7_reap_pid" "$task7_reap_status"
    return 0
  }
  task7_shutdown_exact_pid() {
    local task7_shutdown_pid=${1:-}
    local task7_shutdown_label=${2:-runtime}
    local task7_shutdown_signal
    local task7_shutdown_attempt
    local task7_shutdown_active_status
    [ -n "$task7_shutdown_pid" ] || return 0
    task7_exact_job_active "$task7_shutdown_pid"
    task7_shutdown_active_status=$?
    case "$task7_shutdown_active_status" in
      0) ;;
      1) task7_reap_exact_job "$task7_shutdown_pid" "$task7_shutdown_label"; return 0 ;;
      *) return 1 ;;
    esac
    for task7_shutdown_signal in INT TERM; do
      kill -"$task7_shutdown_signal" "$task7_shutdown_pid" 2>/dev/null || {
        task7_exact_job_active "$task7_shutdown_pid"
        task7_shutdown_active_status=$?
        [ "$task7_shutdown_active_status" -eq 1 ] \
          && { task7_reap_exact_job "$task7_shutdown_pid" "$task7_shutdown_label"; return 0; }
        echo "failed to signal exact $task7_shutdown_label PID $task7_shutdown_pid with $task7_shutdown_signal" >&2
        return 1
      }
      task7_shutdown_attempt=0
      while [ "$task7_shutdown_attempt" -lt "$task7_shutdown_poll_attempts" ]; do
        task7_exact_job_active "$task7_shutdown_pid"
        task7_shutdown_active_status=$?
        case "$task7_shutdown_active_status" in
          0) ;;
          1) task7_reap_exact_job "$task7_shutdown_pid" "$task7_shutdown_label"; return 0 ;;
          *) return 1 ;;
        esac
        sleep "$task7_shutdown_poll_seconds"
        task7_shutdown_attempt=$((task7_shutdown_attempt + 1))
      done
    done
    echo "exact $task7_shutdown_label PID $task7_shutdown_pid remained alive after bounded INT and TERM shutdown" >&2
    return 1
  }

  task7_preflight() {
  task7_worktree_root=$(git rev-parse --show-toplevel) \
    || { task7_fail 'failed to resolve current worktree root'; return 1; }
  task7_uv_cache="$task7_worktree_root/backend/.uv-cache"
  mkdir -p "$task7_uv_cache" \
    || { task7_fail 'failed to prepare repository-local uv cache'; return 1; }
  test -w "$task7_uv_cache" \
    || { task7_fail 'repository-local uv cache is not writable'; return 1; }
  export UV_CACHE_DIR="$task7_uv_cache"
  task7_git_common_dir=$(git rev-parse --path-format=absolute --git-common-dir) \
    || { task7_fail 'failed to resolve absolute Git common directory'; return 1; }
  task7_main_checkout_root=$(dirname "$task7_git_common_dir")
  task7_repo_compose="$task7_worktree_root/docker-compose.yml"
  task7_main_repo_compose="$task7_main_checkout_root/docker-compose.yml"
  test -f "$task7_repo_compose" && test -f "$task7_main_repo_compose" \
    || { task7_fail 'repository-owned Compose file is absent'; return 1; }
  task7_postgres_candidates=''
  task7_pubsub_candidates=''
  for task7_candidate_id in $(docker ps -q); do
    task7_candidate_name=$(docker inspect "$task7_candidate_id" --format '{{.Name}}') \
      || { task7_fail 'failed to inspect candidate container'; return 1; }
    task7_candidate_name=${task7_candidate_name#/}
    if docker inspect "$task7_candidate_id" \
      | jq -e '.[0].HostConfig.PortBindings["5432/tcp"] // [] | any(.HostPort == "5432")' >/dev/null; then
      task7_postgres_candidates="${task7_postgres_candidates}${task7_candidate_name}"$'\n'
    fi
    if docker inspect "$task7_candidate_id" \
      | jq -e '.[0].HostConfig.PortBindings["8085/tcp"] // [] | any(.HostPort == "58085")' >/dev/null; then
      task7_pubsub_candidates="${task7_pubsub_candidates}${task7_candidate_name}"$'\n'
    fi
  done
  test "$(printf '%s' "$task7_postgres_candidates" | awk 'NF { count++ } END { print count + 0 }')" -eq 1 \
    || { task7_fail 'expected exactly one running container publishing host 5432'; return 1; }
  test "$(printf '%s' "$task7_pubsub_candidates" | awk 'NF { count++ } END { print count + 0 }')" -eq 1 \
    || { task7_fail 'expected exactly one running container publishing host 58085'; return 1; }
  task7_postgres_container=$(printf '%s' "$task7_postgres_candidates" | awk 'NF { print; exit }')
  task7_pubsub_container=$(printf '%s' "$task7_pubsub_candidates" | awk 'NF { print; exit }')
  task7_postgres_id=$(docker inspect "$task7_postgres_container" --format '{{.Id}}') \
    || { task7_fail 'failed to capture Postgres identity'; return 1; }
  task7_pubsub_id=$(docker inspect "$task7_pubsub_container" --format '{{.Id}}') \
    || { task7_fail 'failed to capture Pub/Sub identity'; return 1; }
  task7_postgres_binding=$(docker inspect "$task7_postgres_container" \
    | jq -ce '.[0].HostConfig.PortBindings["5432/tcp"]') \
    || { task7_fail 'failed to capture Postgres binding'; return 1; }
  task7_pubsub_binding=$(docker inspect "$task7_pubsub_container" \
    | jq -ce '.[0].HostConfig.PortBindings["8085/tcp"]') \
    || { task7_fail 'failed to capture Pub/Sub binding'; return 1; }
  task7_pubsub_compose_file=$(docker inspect "$task7_pubsub_container" \
    --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}') \
    || { task7_fail 'failed to capture Pub/Sub Compose config-file label'; return 1; }
  case "$task7_pubsub_compose_file" in
    "$task7_repo_compose"|"$task7_main_repo_compose") ;;
    *) task7_fail 'host 58085 container has an unrelated Compose config-file label'; return 1 ;;
  esac
  task7_shared_oid_before=$(docker exec "$task7_postgres_container" \
    psql -U postgres -d postgres -v ON_ERROR_STOP=1 -Atc "SELECT oid FROM pg_database WHERE datname = 'sre_agent'") \
    || { task7_fail 'failed to capture shared database OID'; return 1; }
  test -n "$task7_shared_oid_before" \
    || { task7_fail 'shared sre_agent database is absent'; return 1; }

  docker inspect "$task7_postgres_container" | jq -e '
    .[0] as $container
    | $container.Config.Image == "postgres:18"
      and $container.Config.Cmd == ["postgres"]
      and ($container.Config.Env | index("POSTGRES_DB=sre_agent") != null)
      and ($container.Config.Env | index("POSTGRES_USER=postgres") != null)
      and ($container.Config.Env | index("POSTGRES_HOST_AUTH_METHOD=trust") != null)
      and ($container.Mounts | any(.Destination == "/var/lib/postgresql" and .RW == true))
      and ($container.State.Status == "running")' >/dev/null \
    || { task7_fail 'host 5432 container does not match inspected project Postgres service'; return 1; }
  docker inspect "$task7_pubsub_container" | jq -e --arg compose "$task7_pubsub_compose_file" '
    .[0] as $container
    | $container.Config.Image == "google/cloud-sdk:578.0.0-emulators"
      and ($container.Config.Cmd | index("--project=sre-agent-local") != null)
      and ($container.Config.Cmd | index("--host-port=0.0.0.0:8085") != null)
      and $container.Config.Labels["com.docker.compose.service"] == "pubsub-emulator"
      and $container.Config.Labels["com.docker.compose.project.config_files"] == $compose
      and ($container.State.Status == "running")' >/dev/null \
    || { task7_fail 'host 58085 container does not match this repository Pub/Sub service'; return 1; }

  test "$(docker exec "$task7_postgres_container" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -Atc \
    "SELECT count(*) FROM pg_database WHERE datname IN ('sre_agent_release_acceptance','sre_agent_release_tests')")" -eq 0 \
    || { task7_fail 'release database already exists'; return 1; }
  task7_preflight_complete='true'
  printf 'postgres=%s binding=%s shared_oid=%s\npubsub=%s binding=%s compose_file=%s\n' \
    "$task7_postgres_container" "$task7_postgres_binding" "$task7_shared_oid_before" \
    "$task7_pubsub_container" "$task7_pubsub_binding" "$task7_pubsub_compose_file"

  }

  task7_step1_migrate_acceptance() {
  task7_require_open_gate 'Step 1 acceptance migrations' || return 1
  task7_mutation_started='true'
  docker exec "$task7_postgres_container" createdb -U postgres sre_agent_release_acceptance \
    || { task7_fail 'acceptance database creation failed; enter Step 6 cleanup'; return 1; }
  docker exec "$task7_postgres_container" createdb -U postgres sre_agent_release_tests \
    || { task7_fail 'test database creation failed; enter Step 6 cleanup'; return 1; }
  (cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade 0002_grafana_normalization_v2) \
    || { task7_fail 'acceptance Backend 0002 failed'; return 1; }
  test "$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -Atc "SELECT version_num FROM alembic_version_backend")" = '0002_grafana_normalization_v2' \
    && test "$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -Atc "SELECT to_regclass('public.alembic_version_rca_worker')")" = '' \
    || { task7_fail 'acceptance versions after Backend 0002 are wrong'; return 1; }
  (cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade 0002_adk_specialist_analysis) \
    || { task7_fail 'acceptance Worker 0002 failed'; return 1; }
  test "$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -AtF '|' -c "SELECT (SELECT version_num FROM alembic_version_backend), (SELECT version_num FROM alembic_version_rca_worker)")" = '0002_grafana_normalization_v2|0002_adk_specialist_analysis' \
    || { task7_fail 'acceptance versions after Worker 0002 are wrong'; return 1; }
  (cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade 0003_non_partition_runtime_tables) \
    || { task7_fail 'acceptance Backend 0003 failed'; return 1; }
  test "$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -AtF '|' -c "SELECT (SELECT version_num FROM alembic_version_backend), (SELECT version_num FROM alembic_version_rca_worker)")" = '0003_non_partition_runtime_tables|0002_adk_specialist_analysis' \
    || { task7_fail 'acceptance versions after Backend 0003 are wrong'; return 1; }
  (cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade 0003_validate_ordinary_runtime_tables) \
    || { task7_fail 'acceptance Worker 0003 failed'; return 1; }
  test "$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -AtF '|' -c "SELECT (SELECT version_num FROM alembic_version_backend), (SELECT version_num FROM alembic_version_rca_worker)")" = '0003_non_partition_runtime_tables|0003_validate_ordinary_runtime_tables' \
    || { task7_fail 'acceptance final versions are wrong'; return 1; }
  }

  task7_run_phase 'preflight' task7_preflight || true
  task7_run_phase 'Step 1 acceptance migrations' task7_step1_migrate_acceptance || true
  ```

  Run each mutation or verification block separately and inspect its exit status before pasting the next block. After the first `createdb`, any non-zero result sets `task7_gate_failed='true'`; do not `exit`, close, or replace the named verification terminal, because Step 6 requires its captured container names, bindings, and shared OID. Skip all remaining Step 1-5 actions and go directly to Step 6. After every migration command, query both version tables through `docker exec "$task7_postgres_container" psql ... --dbname=sre_agent_release_acceptance` and compare them with the expected current gate before continuing. Expected: all commands succeed; never replace a command with `upgrade head`. From this point, every Task 7 `createdb`, `psql`, and `dropdb` command must use the captured Postgres container; every emulator outage/restart must use the captured Pub/Sub container. Never run `docker compose up/exec/stop` in Task 7.

- [ ] **Step 2: Run migration, Backend, Worker, and contract suites (2–5 minutes each command)**

  ```bash
  task7_step2_tests() {
  task7_require_open_gate 'Step 2 tests' || return 1
  (cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade 0002_grafana_normalization_v2) \
    || { task7_fail 'test database Backend 0002 failed'; return 1; }
  (cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade 0002_adk_specialist_analysis) \
    || { task7_fail 'test database Worker 0002 failed'; return 1; }
  (cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade 0003_non_partition_runtime_tables) \
    || { task7_fail 'test database Backend 0003 failed'; return 1; }
  (cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade 0003_validate_ordinary_runtime_tables) \
    || { task7_fail 'test database Worker 0003 failed'; return 1; }
  (cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests uv run pytest tests -v) \
    || { task7_fail 'Backend full tests failed'; return 1; }
  (cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests uv run pytest tests -v) \
    || { task7_fail 'Worker full tests failed'; return 1; }
  uv run --project backend pytest contracts/compatibility-tests -v \
    || { task7_fail 'compatibility tests failed'; return 1; }
  }
  task7_run_phase 'Step 2 tests' task7_step2_tests || true
  ```

  Expected: PASS. A test failure is routed to the owning task and that task repeats its own RED/GREEN evidence before this gate is restarted.

- [ ] **Step 3: Run full static checks (2–5 minutes each command)**

  ```bash
  task7_step3_static() {
  task7_require_open_gate 'Step 3 static checks' || return 1
  (cd backend && uv run ruff check . && uv run pyright) \
    || { task7_fail 'Backend static checks failed'; return 1; }
  (cd rca-worker && uv run ruff check . && uv run pyright) \
    || { task7_fail 'Worker static checks failed'; return 1; }
  git diff --check || { task7_fail 'git diff --check failed'; return 1; }
  }
  task7_run_phase 'Step 3 static checks' task7_step3_static || true
  ```

  Expected: zero Ruff and Pyright errors for both complete projects. A pre-existing error is not waived at this gate: route it to the owning task (or add a narrowly scoped defect task), fix it with RED/GREEN evidence, and restart the release gate.

  Render and parse the deployment surfaces and rerun the canonical evidence smoke before any live runtime starts. These are read-only render/test operations, but they use the same dispatcher and therefore cannot run after an earlier gate failure:

  ```bash
  task7_step3_render_deployments() {
  task7_require_open_gate 'Step 3 deployment renders' || return 1
  task7_compose_default=$(docker compose --env-file .env.compose.example config --format json) \
    || { task7_fail 'default Compose JSON render failed'; return 1; }
  printf '%s\n' "$task7_compose_default" | jq -e '
    (.services | keys) == ["postgres", "pubsub-emulator"]
    and (.services | all(has("env_file") | not))
    and (.services.postgres.ports == [{"mode":"ingress","target":5432,"published":"5432","protocol":"tcp"}])
    and (.services["pubsub-emulator"].ports == [{"mode":"ingress","target":8085,"published":"58085","protocol":"tcp"}])' >/dev/null \
    || { task7_fail 'default Compose services/ports/env_file assertion failed'; return 1; }
  task7_compose_custom=$(POSTGRES_HOST_PORT=15432 PUBSUB_HOST_PORT=18085 \
    docker compose --env-file .env.compose.example config --format json) \
    || { task7_fail 'custom Compose JSON render failed'; return 1; }
  printf '%s\n' "$task7_compose_custom" | jq -e '
    (.services | keys) == ["postgres", "pubsub-emulator"]
    and (.services | all(has("env_file") | not))
    and (.services.postgres.ports[0].published == "15432")
    and (.services["pubsub-emulator"].ports[0].published == "18085")' >/dev/null \
    || { task7_fail 'custom Compose services/ports/env_file assertion failed'; return 1; }

  task7_kustomize_names=$(kubectl kustomize deploy/k8s/base \
    | kubectl create --dry-run=client -f - -o name) \
    || { task7_fail 'Kustomize base render/parse failed'; return 1; }
  test "$(printf '%s\n' "$task7_kustomize_names" | awk '/^deployment.apps\// { print }' | sort)" = \
    $'deployment.apps/sre-agent-backend\ndeployment.apps/sre-agent-frontend\ndeployment.apps/sre-agent-rca-worker' \
    || { task7_fail 'Kustomize base does not retain exactly Backend/Frontend/Worker deployments'; return 1; }
  }

  task7_step3_render_jobs() {
  task7_require_open_gate 'Step 3 migration Job renders' || return 1
  for task7_job_spec in \
    'deploy/k8s/jobs/backend-0002-migration-job.yaml|sre-agent-backend-0002-migration-|sre-agent-backend:latest|0002_grafana_normalization_v2' \
    'deploy/k8s/jobs/backend-0003-migration-job.yaml|sre-agent-backend-0003-migration-|sre-agent-backend:latest|0003_non_partition_runtime_tables' \
    'deploy/k8s/jobs/worker-0002-migration-job.yaml|sre-agent-worker-0002-migration-|sre-agent-rca-worker:latest|0002_adk_specialist_analysis' \
    'deploy/k8s/jobs/worker-0003-migration-job.yaml|sre-agent-worker-0003-migration-|sre-agent-rca-worker:latest|0003_validate_ordinary_runtime_tables'; do
    IFS='|' read -r task7_job_file task7_job_name task7_job_image task7_job_revision <<<"$task7_job_spec"
    task7_job_json=$(kubectl create --dry-run=client -f "$task7_job_file" -o json) \
      || { task7_fail "failed to parse exact Job $task7_job_file"; return 1; }
    printf '%s\n' "$task7_job_json" | jq -e \
      --arg name "$task7_job_name" --arg image "$task7_job_image" --arg revision "$task7_job_revision" '
      .kind == "Job"
      and .metadata.generateName == $name
      and .spec.template.spec.containers == [(.spec.template.spec.containers[0])]
      and .spec.template.spec.containers[0].image == $image
      and .spec.template.spec.containers[0].command == ["alembic", "upgrade", $revision]
      and ([.spec.template.spec.containers[0].command[]] | index("head") == null and index("stamp") == null)' >/dev/null \
      || { task7_fail "Job target is not the exact explicit revision: $task7_job_file"; return 1; }
  done
  }

  task7_step3_canonical_evidence() {
  task7_require_open_gate 'Step 3 canonical evidence smoke' || return 1
  (cd rca-worker && env MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests \
    uv run pytest tests/integration/persistence/test_four_stage_conversion.py::test_clean_and_existing_paths_finish_with_identical_catalog_and_data -v) \
    || { task7_fail 'canonical four-gate evidence smoke failed'; return 1; }
  }

  task7_run_phase 'Step 3 deployment renders' task7_step3_render_deployments || true
  task7_run_phase 'Step 3 migration Job renders' task7_step3_render_jobs || true
  task7_run_phase 'Step 3 canonical evidence smoke' task7_step3_canonical_evidence || true
  ```

- [ ] **Step 4: Run request/recovery and evidence end-to-end smoke (2–5 minutes per phase)**

  Before starting either runtime, seed the exact source configured by `.env.backend-api.example` into the disposable acceptance database. The explicit database argument and first fail-closed assertion are both required; abort if `current_database()` is not exactly `sre_agent_release_acceptance`. Never adapt this command to, or run it against, the shared `sre_agent` database.

  ```bash
  task7_step4_seed() {
  task7_require_open_gate 'Step 4 catalog seed' || return 1
  docker exec "$task7_postgres_container" psql -U postgres --dbname=sre_agent_release_acceptance \
    -v ON_ERROR_STOP=1 \
    -c "DO \$\$ BEGIN IF current_database() <> 'sre_agent_release_acceptance' THEN RAISE EXCEPTION 'refusing catalog seed in database %', current_database(); END IF; END \$\$;" \
    -c "INSERT INTO teams (id, name) VALUES ('10000000-0000-0000-0000-000000000001', 'Local Team') ON CONFLICT DO NOTHING" \
    -c "INSERT INTO projects (id, team_id, name) VALUES ('20000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 'local-project') ON CONFLICT DO NOTHING" \
    -c "INSERT INTO environments (id, project_id, name) VALUES ('30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001', 'local') ON CONFLICT DO NOTHING" \
    -c "INSERT INTO grafana_sources (id, project_id, environment_id, name) VALUES ('50000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', 'local-grafana') ON CONFLICT DO NOTHING" \
    -c "DO \$\$ BEGIN IF (SELECT count(*) FROM grafana_sources AS source JOIN projects AS project ON project.id = source.project_id JOIN teams AS team ON team.id = project.team_id JOIN environments AS environment ON environment.id = source.environment_id AND environment.project_id = source.project_id WHERE source.id = '50000000-0000-0000-0000-000000000001' AND source.name = 'local-grafana' AND source.enabled IS TRUE AND source.project_id = '20000000-0000-0000-0000-000000000001' AND source.environment_id = '30000000-0000-0000-0000-000000000001' AND project.id = '20000000-0000-0000-0000-000000000001' AND project.name = 'local-project' AND project.team_id = '10000000-0000-0000-0000-000000000001' AND team.id = '10000000-0000-0000-0000-000000000001' AND team.name = 'Local Team' AND environment.id = '30000000-0000-0000-0000-000000000001' AND environment.name = 'local' AND environment.project_id = '20000000-0000-0000-0000-000000000001') <> 1 THEN RAISE EXCEPTION 'acceptance Grafana catalog does not exactly match the required enabled source and relationships'; END IF; END \$\$;" \
    -c "SELECT source.id AS source_id, source.name AS source_name, source.enabled, source.project_id, project.name AS project_name, project.team_id, team.name AS team_name, source.environment_id, environment.name AS environment_name, environment.project_id AS environment_project_id FROM grafana_sources AS source JOIN projects AS project ON project.id = source.project_id JOIN teams AS team ON team.id = project.team_id JOIN environments AS environment ON environment.id = source.environment_id AND environment.project_id = source.project_id WHERE source.id = '50000000-0000-0000-0000-000000000001'" \
    || { task7_fail 'Task 7 catalog seed/assertion failed'; return 1; }
  }
  task7_run_phase 'Step 4 catalog seed' task7_step4_seed || true
  ```

  Expected: the assertion succeeds only when exactly one row has all four exact names and UUIDs, `enabled=true`, and the exact team/project/environment relationships shown above; otherwise `ON_ERROR_STOP` aborts before Backend startup. The final query records the asserted row as readable evidence.

  Start the named Backend and Worker as run-owned background jobs from the retained verification shell. Capture their exact PIDs and logs globally; cleanup may signal only those PIDs. A launch failure sets the global gate flag, and all subsequent phases refuse to run:

  ```bash
  task7_start_runtimes() {
  task7_require_open_gate 'Step 4 start runtimes' || return 1
  task7_runtime_log_dir=$(mktemp -d /tmp/task7-runtime.XXXXXX) \
    || { task7_fail 'unable to create Task 7 runtime log directory'; return 1; }
  (cd backend && exec env DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance BACKEND_ENV_FILE=../.env.backend-api.example .venv/bin/uvicorn sre_agent.api.main:app --host 127.0.0.1 --port 8000) \
    >"$task7_runtime_log_dir/backend.log" 2>&1 &
  task7_backend_pid=$!
  (cd rca-worker && exec env DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance RCA_WORKER_ENV_FILE=../.env.rca-worker.example .venv/bin/sre-agent-rca-worker) \
    >"$task7_runtime_log_dir/worker.log" 2>&1 &
  task7_worker_pid=$!
  test -n "$task7_backend_pid" && test -n "$task7_worker_pid" \
    || { task7_fail 'failed to capture run-owned runtime PIDs'; return 1; }
  }
  task7_run_phase 'Step 4 start runtimes' task7_start_runtimes || true
  ```

  In the retained verification terminal, prove Worker bootstrap and Backend readiness, capture pre-first counts, submit the canonical fixture with body plus status, parse the returned `deliveryId`, and follow that exact delivery through the actual schema joins. Do not establish the second-request baseline until the delivery-linked event is `PUBLISHED`, its job is `SUCCEEDED`, exactly one report exists, and that report has non-null `result_status`:

  ```bash
  task7_step4_first_request() {
  task7_require_open_gate 'Step 4 first request' || return 1
  task7_worker_bootstrapped='false'
  task7_backend_ready='false'
  for task7_attempt in {1..30}; do
    if curl -fsS 'http://127.0.0.1:58085/v1/projects/sre-agent-local/topics/rca-jobs' >/dev/null \
      && curl -fsS 'http://127.0.0.1:58085/v1/projects/sre-agent-local/subscriptions/rca-jobs-local-sub' >/dev/null; then
      task7_worker_bootstrapped='true'; break
    fi
    sleep 1
  done
  test "$task7_worker_bootstrapped" = 'true' \
    || { task7_fail 'Worker did not bootstrap exact topic/subscription before first request'; return 1; }
  for task7_attempt in {1..30}; do
    if curl -fsS 'http://127.0.0.1:8000/health/ready' >/dev/null; then
      task7_backend_ready='true'; break
    fi
    sleep 1
  done
  test "$task7_backend_ready" = 'true' \
    || { task7_fail 'Backend did not become ready before first request'; return 1; }

  task7_pre_first=$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -AtF '|' -c \
    "SELECT (SELECT count(*) FROM webhook_deliveries), (SELECT count(*) FROM outbox_events),
            (SELECT count(*) FROM rca_runs), (SELECT count(*) FROM worker_jobs), (SELECT count(*) FROM rca_reports)") \
    || { task7_fail 'unable to capture pre-first counts'; return 1; }
  IFS='|' read -r task7_deliveries_pre_first task7_outbox_pre_first task7_runs_pre_first task7_jobs_pre_first task7_reports_pre_first <<<"$task7_pre_first"
  task7_first_response=$(curl --fail-with-body --show-error --silent \
    --write-out '\nHTTP_STATUS=%{http_code}\n' -X POST \
    'http://127.0.0.1:8000/webhooks/v1/grafana/50000000-0000-0000-0000-000000000001' \
    -H 'Authorization: Bearer replace-me' -H 'Content-Type: application/json' \
    --data-binary @contracts/examples/grafana-firing.json) \
    || { task7_fail 'first canonical request failed'; return 1; }
  task7_first_status=${task7_first_response##*HTTP_STATUS=}
  task7_first_body=${task7_first_response%$'\n'HTTP_STATUS=*}
  test "$task7_first_status" = '202' \
    || { task7_fail 'first canonical request did not return HTTP 202'; return 1; }
  task7_first_delivery_id=$(printf '%s\n' "$task7_first_body" | jq -er '.deliveryId') \
    || { task7_fail 'first response has no valid deliveryId'; return 1; }

  task7_first_complete='false'
  for task7_attempt in {1..120}; do
    task7_first_tuple=$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -AtF '|' -c \
      "SELECT event.id, event.status, run.id, job.id, job.status,
              count(report.id), count(report.id) FILTER (WHERE report.result_status IS NOT NULL)
         FROM webhook_deliveries AS delivery
         JOIN alert_events AS alert ON alert.delivery_id = delivery.id
         JOIN incident_alerts AS link ON link.alert_event_id = alert.id
         JOIN rca_runs AS run ON run.incident_id = link.incident_id
         JOIN worker_jobs AS job ON job.rca_run_id = run.id
         JOIN outbox_events AS event ON event.payload->>'rcaRunId' = run.id::text
         LEFT JOIN rca_reports AS report ON report.rca_run_id = run.id
        WHERE delivery.id = '$task7_first_delivery_id'
        GROUP BY event.id, event.status, run.id, job.id, job.status") \
      || { task7_fail 'unable to query first delivery-linked state'; return 1; }
    if [ "$(printf '%s\n' "$task7_first_tuple" | awk 'NF { n++ } END { print n + 0 }')" -eq 1 ]; then
      IFS='|' read -r task7_first_event_id task7_first_event_status task7_first_run_id task7_first_job_id task7_first_job_status task7_first_report_count task7_first_result_count <<<"$task7_first_tuple"
      if [ "$task7_first_event_status" = 'PUBLISHED' ] \
        && [ "$task7_first_job_status" = 'SUCCEEDED' ] \
        && [ "$task7_first_report_count" = '1' ] \
        && [ "$task7_first_result_count" = '1' ]; then
        task7_first_complete='true'; break
      fi
    fi
    sleep 1
  done
  test "$task7_first_complete" = 'true' \
    || { task7_fail 'first delivery did not reach one PUBLISHED event, SUCCEEDED job, and non-null-status report'; return 1; }
  task7_post_first=$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -AtF '|' -c \
    "SELECT (SELECT count(*) FROM webhook_deliveries), (SELECT count(*) FROM outbox_events),
            (SELECT count(*) FROM rca_runs), (SELECT count(*) FROM worker_jobs), (SELECT count(*) FROM rca_reports)") \
    || { task7_fail 'unable to capture post-first counts'; return 1; }
  IFS='|' read -r task7_deliveries_post_first task7_outbox_before task7_runs_before task7_jobs_before task7_reports_post_first <<<"$task7_post_first"
  test "$task7_deliveries_post_first" -eq $((task7_deliveries_pre_first + 1)) \
    && test "$task7_outbox_before" -eq $((task7_outbox_pre_first + 1)) \
    && test "$task7_runs_before" -eq $((task7_runs_pre_first + 1)) \
    && test "$task7_jobs_before" -eq $((task7_jobs_pre_first + 1)) \
    && test "$task7_reports_post_first" -eq $((task7_reports_pre_first + 1)) \
    || { task7_fail 'first request did not increase every delivery/work/report count exactly once'; return 1; }
  }
  task7_run_phase 'Step 4 first request' task7_step4_first_request || true
  ```

  Expected: HTTP `202`; exactly one outbox event reaches `PUBLISHED`; Worker claims its job and persists a report with non-null `result_status`. Query `outbox_events`, `worker_jobs`, and `rca_reports` in `sre_agent_release_acceptance` for the returned delivery/run IDs. The example Worker env intentionally sets `SPECIALIST_ANALYSIS_MODE=DISABLED`, so this live phase does not assert that evidence exists; exact non-UTF-8 `raw_result`, provenance `metadata`, content hash, and UUID-only `hypothesis_evidence` are proven by the canonical four-gate persistence smoke executed in Step 2 and its output must be included in release evidence.

  Keep the original named `Task 7 verification` terminal open for the rest of this phase. The preceding function has established the only valid post-first baseline. The second-request/recovery function below is one fail-closed phase; every failure returns immediately, and the dispatcher refuses every later phase once the global flag is set.

  ```bash
  task7_step4_recovery() {
  task7_require_open_gate 'Step 4 recovery' || return 1
  printf 'post-first baseline outbox=%s runs=%s jobs=%s\n' "$task7_outbox_before" "$task7_runs_before" "$task7_jobs_before"
  }
  task7_run_phase 'Step 4 recovery preflight' task7_step4_recovery || true
  ```

  Stop only the captured `pubsub-emulator`, then stream a deterministic transformation of `contracts/examples/grafana-firing-aws.json` into the second request from the `Task 7 verification` terminal. Preserve the canonical fixture on disk. The transformed request must use the distinct alert name `High CPU usage recovery smoke` consistently in the alert labels, grouping envelope, title/message, and a distinct deterministic fingerprint. This is required because incident identity v2 groups by exact source + folder + alert name; changing provider detection or fingerprint alone does not create a second incident/run. The explicit guarded pipeline makes either `jq` or `curl` failure stop the phase while retaining the terminal variables needed by Step 6.

  ```bash
  task7_step4_create_failed_event() {
  task7_require_open_gate 'Step 4 create failed event' || return 1
  test "$(docker inspect "$task7_pubsub_container" --format '{{.State.Status}}')" = 'running' \
    && test "$(docker inspect "$task7_pubsub_container" --format '{{.Id}}')" = "$task7_pubsub_id" \
    && test "$(docker inspect "$task7_pubsub_container" | jq -ce '.[0].HostConfig.PortBindings["8085/tcp"]')" = "$task7_pubsub_binding" \
    && docker inspect "$task7_pubsub_container" | jq -e --arg compose "$task7_pubsub_compose_file" '
         .[0].Config.Image == "google/cloud-sdk:578.0.0-emulators"
         and (.[0].Config.Cmd | index("--project=sre-agent-local") != null)
         and (.[0].Config.Cmd | index("--host-port=0.0.0.0:8085") != null)
         and .[0].Config.Labels["com.docker.compose.service"] == "pubsub-emulator"
         and .[0].Config.Labels["com.docker.compose.project.config_files"] == $compose' >/dev/null \
    || { echo 'captured Pub/Sub container is not the original running binding' >&2; task7_gate_failed='true'; return 1; }
  docker stop "$task7_pubsub_id" >/dev/null \
    || { echo 'failed to stop captured Pub/Sub container' >&2; task7_gate_failed='true'; return 1; }
  test "$(docker inspect "$task7_pubsub_container" --format '{{.State.Status}}')" = 'exited' \
    && test "$(docker inspect "$task7_pubsub_container" --format '{{.Id}}')" = "$task7_pubsub_id" \
    || { echo 'captured Pub/Sub container did not stop' >&2; task7_gate_failed='true'; return 1; }
  if ! task7_second_response=$(
    set -o pipefail
    jq -e -c '
        .alerts |= map(
          .labels.alertname = "High CPU usage recovery smoke"
          | .fingerprint = "c6eadffa33fcdf38"
        )
        | .groupLabels.alertname = "High CPU usage recovery smoke"
        | .groupKey = "{}:{alertname=\"High CPU usage recovery smoke\"}"
        | .title = "[FIRING:1] High CPU usage recovery smoke"
        | .message |= gsub("High CPU usage"; "High CPU usage recovery smoke")
      ' contracts/examples/grafana-firing-aws.json \
        | curl --fail-with-body --show-error --silent \
            --write-out '\nHTTP_STATUS=%{http_code}\n' \
            -X POST 'http://127.0.0.1:8000/webhooks/v1/grafana/50000000-0000-0000-0000-000000000001' \
            -H 'Authorization: Bearer replace-me' \
            -H 'Content-Type: application/json' \
            --data-binary @-
  ); then
    echo 'Task 7 transformed request failed; recovery is forbidden' >&2
    task7_gate_failed='true'
    return 1
  fi
  printf '%s\n' "$task7_second_response"
  case "$task7_second_response" in
    *'HTTP_STATUS=202') ;;
    *) echo 'Task 7 transformed request did not return HTTP 202' >&2; task7_gate_failed='true'; return 1 ;;
  esac
  }
  task7_run_phase 'Step 4 create failed event' task7_step4_create_failed_event || true
  ```

  Still in `Task 7 verification`, assert that the transaction added exactly one run, job, and outbox row; capture that new event/run/job tuple and require the event to be `FAILED`. Do not stop or restart a runtime until this passes.

  ```bash
  task7_step4_capture_failed_event() {
  task7_require_open_gate 'Step 4 capture failed event' || return 1
  task7_after_second=$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -AtF '|' -c \
    "SELECT (SELECT count(*) FROM outbox_events), (SELECT count(*) FROM rca_runs), (SELECT count(*) FROM worker_jobs)") \
    || { echo 'unable to query post-request counts' >&2; task7_gate_failed='true'; return 1; }
  IFS='|' read -r task7_outbox_after task7_runs_after task7_jobs_after <<<"$task7_after_second"
  test "$task7_outbox_after" -eq $((task7_outbox_before + 1)) \
    && test "$task7_runs_after" -eq $((task7_runs_before + 1)) \
    && test "$task7_jobs_after" -eq $((task7_jobs_before + 1)) \
    || { echo 'second request did not add exactly one outbox/run/job' >&2; task7_gate_failed='true'; return 1; }

  task7_failed_tuple=$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -AtF '|' -c \
    "SELECT event.id, event.status, event.payload->>'rcaRunId', job.id
       FROM outbox_events AS event
       JOIN worker_jobs AS job ON job.rca_run_id = (event.payload->>'rcaRunId')::uuid
      ORDER BY event.created_at DESC, event.id DESC LIMIT 1") \
    || { echo 'unable to capture failed event tuple' >&2; task7_gate_failed='true'; return 1; }
  IFS='|' read -r task7_failed_event_id task7_failed_status task7_failed_run_id task7_failed_job_id <<<"$task7_failed_tuple"
  test "$task7_failed_status" = 'FAILED' \
    && test -n "$task7_failed_event_id" \
    && test -n "$task7_failed_run_id" \
    && test -n "$task7_failed_job_id" \
    || { echo 'new outbox tuple is absent or not FAILED' >&2; task7_gate_failed='true'; return 1; }
  printf 'failed event=%s run=%s job=%s\n' "$task7_failed_event_id" "$task7_failed_run_id" "$task7_failed_job_id"
  }
  task7_run_phase 'Step 4 capture failed event' task7_step4_capture_failed_event || true
  ```

  Stop only the exact run-owned PIDs and wait for both, then restart the immutable emulator by ID. Never use `pkill`, `killall`, or a name pattern:

  ```bash
  task7_step4_restart_emulator() {
  task7_require_open_gate 'Step 4 restart emulator' || return 1
  task7_shutdown_exact_pid "$task7_backend_pid" 'Backend' \
    || { task7_fail 'exact Backend process did not stop within the bounded shutdown'; return 1; }
  task7_backend_pid=''
  task7_shutdown_exact_pid "$task7_worker_pid" 'Worker' \
    || { task7_fail 'exact Worker process did not stop within the bounded shutdown'; return 1; }
  task7_worker_pid=''
  test "$(docker inspect "$task7_pubsub_container" --format '{{.State.Status}}')" = 'exited' \
    && test "$(docker inspect "$task7_pubsub_container" --format '{{.Id}}')" = "$task7_pubsub_id" \
    && test "$(docker inspect "$task7_pubsub_container" | jq -ce '.[0].HostConfig.PortBindings["8085/tcp"]')" = "$task7_pubsub_binding" \
    && docker inspect "$task7_pubsub_container" | jq -e --arg compose "$task7_pubsub_compose_file" '
         .[0].Config.Image == "google/cloud-sdk:578.0.0-emulators"
         and (.[0].Config.Cmd | index("--project=sre-agent-local") != null)
         and (.[0].Config.Cmd | index("--host-port=0.0.0.0:8085") != null)
         and .[0].Config.Labels["com.docker.compose.service"] == "pubsub-emulator"
         and .[0].Config.Labels["com.docker.compose.project.config_files"] == $compose' >/dev/null \
    || { echo 'captured Pub/Sub container is not the original stopped binding' >&2; task7_gate_failed='true'; return 1; }
  docker start "$task7_pubsub_id" >/dev/null \
    || { echo 'failed to restart captured Pub/Sub container' >&2; task7_gate_failed='true'; return 1; }
  task7_emulator_ready='false'
  for task7_attempt in {1..30}; do
    if task7_topics=$(curl -fsS 'http://127.0.0.1:58085/v1/projects/sre-agent-local/topics'); then
      task7_emulator_ready='true'
      printf 'emulator ready: %s\n' "$task7_topics"
      break
    fi
    sleep 1
  done
  test "$task7_emulator_ready" = 'true' \
    || { echo 'Pub/Sub emulator did not become ready within 30 seconds' >&2; task7_gate_failed='true'; return 1; }
  }
  task7_run_phase 'Step 4 restart emulator' task7_step4_restart_emulator || true
  ```

  Restart Worker first and Backend second as new run-owned jobs, recapturing both exact PIDs before readiness checks:

  ```bash
  task7_step4_restart_runtimes() {
  task7_require_open_gate 'Step 4 restart runtimes' || return 1
  (cd rca-worker && exec env DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance RCA_WORKER_ENV_FILE=../.env.rca-worker.example .venv/bin/sre-agent-rca-worker) \
    >>"$task7_runtime_log_dir/worker.log" 2>&1 &
  task7_worker_pid=$!
  task7_worker_bootstrapped='false'
  for task7_attempt in {1..30}; do
    if curl -fsS 'http://127.0.0.1:58085/v1/projects/sre-agent-local/topics/rca-jobs' >/dev/null \
      && curl -fsS 'http://127.0.0.1:58085/v1/projects/sre-agent-local/subscriptions/rca-jobs-local-sub' >/dev/null; then
      task7_worker_bootstrapped='true'
      break
    fi
    sleep 1
  done
  test "$task7_worker_bootstrapped" = 'true' \
    || { echo 'Worker did not bootstrap topic/subscription within 30 seconds' >&2; task7_gate_failed='true'; return 1; }

  (cd backend && exec env DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance BACKEND_ENV_FILE=../.env.backend-api.example .venv/bin/uvicorn sre_agent.api.main:app --host 127.0.0.1 --port 8000) \
    >>"$task7_runtime_log_dir/backend.log" 2>&1 &
  task7_backend_pid=$!
  task7_backend_ready='false'
  for task7_attempt in {1..30}; do
    if curl -fsS 'http://127.0.0.1:8000/health/ready' >/dev/null; then
      task7_backend_ready='true'
      break
    fi
    sleep 1
  done
  test "$task7_backend_ready" = 'true' \
    || { echo 'Backend did not become ready within 30 seconds' >&2; task7_gate_failed='true'; return 1; }
  }
  task7_run_phase 'Step 4 restart runtimes' task7_step4_restart_runtimes || true
  ```

  Before recovery, require the same captured event to remain `FAILED` and all three counts to remain unchanged. This proves startup did not auto-replay and did not create a duplicate run/job/outbox. Only after this assertion may the protected manual recovery call run. Require a non-zero `selected` and `published` response with no publish failure:

  ```bash
  task7_step4_manual_recovery() {
  task7_require_open_gate 'Step 4 manual recovery' || return 1
  task7_pre_recovery=$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -AtF '|' -c \
    "SELECT (SELECT status FROM outbox_events WHERE id = '$task7_failed_event_id'),
            (SELECT count(*) FROM outbox_events), (SELECT count(*) FROM rca_runs), (SELECT count(*) FROM worker_jobs)") \
    || { echo 'unable to query pre-recovery state' >&2; task7_gate_failed='true'; return 1; }
  IFS='|' read -r task7_pre_status task7_pre_outbox task7_pre_runs task7_pre_jobs <<<"$task7_pre_recovery"
  test "$task7_pre_status" = 'FAILED' \
    && test "$task7_pre_outbox" -eq "$task7_outbox_after" \
    && test "$task7_pre_runs" -eq "$task7_runs_after" \
    && test "$task7_pre_jobs" -eq "$task7_jobs_after" \
    || { echo 'startup replayed the event or created duplicate work' >&2; task7_gate_failed='true'; return 1; }

  if ! task7_recovery_response=$(curl --fail-with-body --show-error --silent \
    -X POST 'http://127.0.0.1:8000/api/v1/operations/outbox-events/retry-failed?limit=100' \
    -H 'Authorization: Bearer local-operator'); then
    echo 'protected manual recovery request failed' >&2
    task7_gate_failed='true'
    return 1
  fi
  printf '%s\n' "$task7_recovery_response"
  printf '%s\n' "$task7_recovery_response" \
    | jq -e '.selected > 0 and .published > 0 and .failed == 0' >/dev/null \
    || { echo 'manual recovery did not publish the selected failed event' >&2; task7_gate_failed='true'; return 1; }
  }
  task7_run_phase 'Step 4 manual recovery' task7_step4_manual_recovery || true
  ```

  Finally, boundedly wait for the captured job to succeed and persist a report, then require the same event to be `PUBLISHED` and all counts still equal the post-second-request values. This is the terminal-processing and no-duplicate assertion; it must pass before Step 5.

  ```bash
  task7_step4_terminal_processing() {
  task7_require_open_gate 'Step 4 terminal processing' || return 1
  task7_processing_complete='false'
  for task7_attempt in {1..120}; do
    if ! task7_job_state=$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -AtF '|' -c \
      "SELECT job.status, count(report.id)
         FROM worker_jobs AS job
         LEFT JOIN rca_reports AS report ON report.rca_run_id = job.rca_run_id
        WHERE job.id = '$task7_failed_job_id'
        GROUP BY job.status"); then
      echo 'unable to query recovered Worker job state' >&2
      task7_gate_failed='true'
      break
    fi
    if [ "$task7_job_state" = 'SUCCEEDED|1' ]; then
      task7_processing_complete='true'
      break
    fi
    sleep 1
  done
  test "$task7_processing_complete" = 'true' \
    || { echo 'recovered Worker job did not succeed with one report within 120 seconds' >&2; task7_gate_failed='true'; return 1; }

  task7_final_state=$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -AtF '|' -c \
    "SELECT (SELECT status FROM outbox_events WHERE id = '$task7_failed_event_id'),
            (SELECT count(*) FROM outbox_events), (SELECT count(*) FROM rca_runs), (SELECT count(*) FROM worker_jobs)") \
    || { echo 'unable to query terminal recovery state' >&2; task7_gate_failed='true'; return 1; }
  IFS='|' read -r task7_final_status task7_final_outbox task7_final_runs task7_final_jobs <<<"$task7_final_state"
  test "$task7_final_status" = 'PUBLISHED' \
    && test "$task7_final_outbox" -eq "$task7_outbox_after" \
    && test "$task7_final_runs" -eq "$task7_runs_after" \
    && test "$task7_final_jobs" -eq "$task7_jobs_after" \
    || { echo 'terminal recovery state is not published and duplicate-free' >&2; task7_gate_failed='true'; return 1; }
  }
  task7_run_phase 'Step 4 terminal processing' task7_step4_terminal_processing || true
  ```

  Expected: the streamed payload reports HTTP `202` and creates a distinct incident/run because its source + folder + alert name identity differs from the first request; its new outbox event is `FAILED` while the emulator is unavailable. Restart does not auto-replay it. Protected recovery returns non-zero `selected`/`published` without payload overrides, the captured Worker job succeeds with one report, and at-least-once processing creates no duplicate RCA run/job/outbox.

- [ ] **Step 5: Capture catalog and rollback-policy evidence (2–5 minutes)**

  ```bash
  task7_step5_catalog() {
  task7_require_open_gate 'Step 5 catalog' || return 1
  task7_catalog=$(docker exec "$task7_postgres_container" psql -U postgres -d sre_agent_release_acceptance -v ON_ERROR_STOP=1 -AtF '|' -c "
  SELECT c.relname, c.relkind, c.relispartition
  FROM pg_class AS c
  WHERE c.relname IN (
      'webhook_deliveries','alert_events','evidence_records','incident_messages',
      'incident_timeline_events','audit_events',
      'webhook_deliveries__partitioned_legacy_0003',
      'alert_events__partitioned_legacy_0003',
      'evidence_records__partitioned_legacy_0003',
      'incident_messages__partitioned_legacy_0003',
      'incident_timeline_events__partitioned_legacy_0003',
      'audit_events__partitioned_legacy_0003'
  ) ORDER BY c.relname") \
    || { task7_fail 'unable to capture Task 7 relation catalog'; return 1; }
  test "$(printf '%s\n' "$task7_catalog" | awk -F '|' '$1 !~ /__partitioned_legacy_0003$/ && $2 == "r" && $3 == "f" { n++ } END { print n + 0 }')" -eq 6 \
    && test "$(printf '%s\n' "$task7_catalog" | awk -F '|' '$1 ~ /__partitioned_legacy_0003$/ && $2 == "p" && $3 == "t" { n++ } END { print n + 0 }')" -eq 6 \
    && test "$(printf '%s\n' "$task7_catalog" | awk 'NF { n++ } END { print n + 0 }')" -eq 12 \
    || { task7_fail 'catalog must be six canonical ordinary tables plus six retained legacy partitioned parents'; return 1; }
  printf '%s\n' "$task7_catalog"
  }
  task7_run_phase 'Step 5 catalog' task7_step5_catalog || true
  ```

  Expected: exactly six canonical `relkind='r'`/`relispartition=false` relations and exactly six retained `__partitioned_legacy_0003` parents with `relkind='p'`/`relispartition=true`, total twelve. Record that no automatic downgrade or legacy cleanup has been executed.

- [ ] **Step 6: Clean disposable databases and commit only task-owned defect fixes (2–5 minutes)**

  This is a mandatory `finally` path after success or any failure in Steps 1–5, including a partially created database or a failed transformed request. Return to the still-open `Task 7 verification` terminal; never discard or reconstruct its captured names/bindings/OID/PIDs. Set `task7_cleanup_failed='false'` and attempt every applicable cleanup subsection even if an earlier cleanup assertion fails, recording `task7_cleanup_failed='true'` instead of exiting. If preflight failed before `task7_mutation_started` became true, take the bounded, read-only branch below: confirm any already captured identities are unchanged, confirm no release database exists, check Pub/Sub health once, and skip the mutation-only cleanup blocks. Any failure after the first database mutation must still take every full cleanup subsection. For full cleanup, use the initialization helper to signal only non-empty run-owned Backend/Worker PIDs with bounded INT-then-TERM shutdown, then boundedly require port 8000 to be clear. Never use `pkill`, `killall`, a name pattern, `SIGKILL`, or terminate an unrelated process.

  ```bash
  task7_cleanup_failed='false'
  if [ "${task7_mutation_started:-false}" = 'false' ]; then
    printf 'preflight ended before mutation; performing read-only final-state confirmation\n'
    if [ -n "${task7_postgres_container:-}" ]; then
      test "$(docker inspect "$task7_postgres_container" --format '{{.State.Status}}')" = 'running' \
        && test "$(docker inspect "$task7_postgres_container" --format '{{.Id}}')" = "$task7_postgres_id" \
        && test "$(docker inspect "$task7_postgres_container" | jq -ce '.[0].HostConfig.PortBindings["5432/tcp"]')" = "$task7_postgres_binding" \
        && test "$(docker exec "$task7_postgres_container" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -Atc \
          "SELECT count(*) FROM pg_database WHERE datname IN ('sre_agent_release_acceptance','sre_agent_release_tests')")" -eq 0 \
        && { [ -z "${task7_shared_oid_before:-}" ] \
          || test "$(docker exec "$task7_postgres_container" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -Atc \
            "SELECT oid FROM pg_database WHERE datname = 'sre_agent'")" = "$task7_shared_oid_before"; } \
        || { echo 'read-only Postgres final-state confirmation failed' >&2; task7_cleanup_failed='true'; }
    else
      printf 'preflight failed before Postgres identity capture; no Task 7 mutation was attempted\n'
    fi
    if [ -n "${task7_pubsub_container:-}" ]; then
      test "$(docker inspect "$task7_pubsub_container" --format '{{.State.Status}}')" = 'running' \
        && test "$(docker inspect "$task7_pubsub_container" --format '{{.Id}}')" = "$task7_pubsub_id" \
        && test "$(docker inspect "$task7_pubsub_container" | jq -ce '.[0].HostConfig.PortBindings["8085/tcp"]')" = "$task7_pubsub_binding" \
        && test "$(docker inspect "$task7_pubsub_container" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')" = "$task7_pubsub_compose_file" \
        && curl -fsS 'http://127.0.0.1:58085/v1/projects/sre-agent-local/topics' >/dev/null \
        || { echo 'read-only Pub/Sub final-state confirmation failed' >&2; task7_cleanup_failed='true'; }
    else
      printf 'preflight failed before Pub/Sub identity capture; no Task 7 mutation was attempted\n'
    fi
  else
    if task7_shutdown_exact_pid "${task7_backend_pid:-}" 'Backend'; then
      task7_backend_pid=''
    else
      echo 'failed to stop exact Task 7 Backend PID during cleanup' >&2
      task7_cleanup_failed='true'
    fi
    if task7_shutdown_exact_pid "${task7_worker_pid:-}" 'Worker'; then
      task7_worker_pid=''
    else
      echo 'failed to stop exact Task 7 Worker PID during cleanup' >&2
      task7_cleanup_failed='true'
    fi
    task7_backend_clear='false'
    for task7_attempt in {1..30}; do
      if ! lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null; then
        task7_backend_clear='true'
        break
      fi
      sleep 1
    done
    test "$task7_backend_clear" = 'true' \
      || { echo 'the exact Task 7 Backend process has not released port 8000' >&2; task7_cleanup_failed='true'; }
  fi
  ```

  Require the originally captured Postgres container to be running and unchanged, list only the shared/release catalog, then drop only the two literal release database names through that container. `--if-exists` makes this safe for partial-failure cleanup; it does not broaden the target.

  ```bash
  if [ "$task7_mutation_started" = 'true' ]; then
    task7_postgres_safe='true'
    test "$(docker inspect "$task7_postgres_container" --format '{{.State.Status}}')" = 'running' \
      || { echo 'captured Postgres container is not running; do not substitute another container' >&2; task7_postgres_safe='false'; task7_cleanup_failed='true'; }
    test "$(docker inspect "$task7_postgres_container" --format '{{.Id}}')" = "$task7_postgres_id" \
      || { echo 'captured Postgres container identity changed' >&2; task7_postgres_safe='false'; task7_cleanup_failed='true'; }
    test "$(docker inspect "$task7_postgres_container" | jq -ce '.[0].HostConfig.PortBindings["5432/tcp"]')" = "$task7_postgres_binding" \
      || { echo 'captured Postgres binding changed' >&2; task7_postgres_safe='false'; task7_cleanup_failed='true'; }
    if [ "$task7_postgres_safe" = 'true' ]; then
      docker exec "$task7_postgres_container" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c \
        "SELECT datname, oid FROM pg_database WHERE datname IN ('sre_agent','sre_agent_release_acceptance','sre_agent_release_tests') ORDER BY datname" \
        || { echo 'failed to list exact Task 7 database catalog' >&2; task7_cleanup_failed='true'; }
      docker exec "$task7_postgres_container" dropdb -U postgres --if-exists --force sre_agent_release_acceptance \
        || { echo 'failed to drop exact acceptance database' >&2; task7_cleanup_failed='true'; }
      docker exec "$task7_postgres_container" dropdb -U postgres --if-exists --force sre_agent_release_tests \
        || { echo 'failed to drop exact test database' >&2; task7_cleanup_failed='true'; }
      task7_final_catalog=$(docker exec "$task7_postgres_container" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -AtF '|' -c \
        "SELECT datname, oid FROM pg_database WHERE datname IN ('sre_agent','sre_agent_release_acceptance','sre_agent_release_tests') ORDER BY datname") \
        || { echo 'failed to read final Task 7 database catalog' >&2; task7_cleanup_failed='true'; }
      test "$task7_final_catalog" = "sre_agent|$task7_shared_oid_before" \
        || { echo 'release databases remain or shared sre_agent OID changed' >&2; task7_cleanup_failed='true'; }
    else
      echo 'skipping database cleanup because captured Postgres ownership changed' >&2
    fi
  fi
  ```

  Finally, restore only the originally captured Pub/Sub container if the failure path left it stopped, then boundedly require the same exact name, original binding, repository ownership labels, and health endpoint. This cleanup restoration is not permission to continue a failed recovery phase.

  ```bash
  if [ "$task7_mutation_started" = 'true' ]; then
    task7_pubsub_safe='true'
    task7_pubsub_status=$(docker inspect "$task7_pubsub_container" --format '{{.State.Status}}') \
      || { echo 'failed to inspect captured Pub/Sub container during cleanup' >&2; task7_pubsub_safe='false'; task7_cleanup_failed='true'; }
    test "$(docker inspect "$task7_pubsub_container" | jq -ce '.[0].HostConfig.PortBindings["8085/tcp"]')" = "$task7_pubsub_binding" \
      || { echo 'captured Pub/Sub binding changed' >&2; task7_pubsub_safe='false'; task7_cleanup_failed='true'; }
    test "$(docker inspect "$task7_pubsub_container" --format '{{.Id}}')" = "$task7_pubsub_id" \
      || { echo 'captured Pub/Sub container identity changed' >&2; task7_pubsub_safe='false'; task7_cleanup_failed='true'; }
    docker inspect "$task7_pubsub_container" | jq -e --arg compose "$task7_pubsub_compose_file" '
      .[0].Config.Image == "google/cloud-sdk:578.0.0-emulators"
      and (.[0].Config.Cmd | index("--project=sre-agent-local") != null)
      and (.[0].Config.Cmd | index("--host-port=0.0.0.0:8085") != null)
      and .[0].Config.Labels["com.docker.compose.service"] == "pubsub-emulator"
      and .[0].Config.Labels["com.docker.compose.project.config_files"] == $compose' >/dev/null \
      || { echo 'captured Pub/Sub ownership changed' >&2; task7_pubsub_safe='false'; task7_cleanup_failed='true'; }
    if [ "$task7_pubsub_safe" = 'true' ] && [ "$task7_pubsub_status" != 'running' ]; then
      docker start "$task7_pubsub_id" >/dev/null \
        || { echo 'failed to restore captured Pub/Sub container during cleanup' >&2; task7_cleanup_failed='true'; }
    fi
    task7_cleanup_pubsub_ready='false'
    for task7_attempt in {1..30}; do
      if [ "$task7_pubsub_safe" = 'true' ] \
        && curl -fsS 'http://127.0.0.1:58085/v1/projects/sre-agent-local/topics' >/dev/null; then
        task7_cleanup_pubsub_ready='true'
        break
      fi
      sleep 1
    done
    test "$task7_cleanup_pubsub_ready" = 'true' \
      || { echo 'captured Pub/Sub container is not healthy after cleanup' >&2; task7_cleanup_failed='true'; }
    printf 'cleanup preserved postgres=%s shared_oid=%s pubsub=%s binding=%s compose_file=%s\n' \
      "$task7_postgres_container" "$task7_shared_oid_before" "$task7_pubsub_container" "$task7_pubsub_binding" "$task7_pubsub_compose_file"
  else
    printf 'read-only cleanup confirmation complete; no Task 7 mutation occurred\n'
  fi
  test "$task7_gate_failed" = 'false' \
    && test "$task7_cleanup_failed" = 'false'
  ```

  If no defect files changed, do not create a verification-only source commit. If a defect is found, return it to its owning Task 1–6 file list, commit with that task’s message family, rerun its focused suite, then restart this release gate from Step 1. Store non-secret command output with release evidence outside the repository.

## Plan Self-Review

- **Spec coverage:** Task 1 implements true Worker 0002 preservation and fail-closed Backend conversion. Task 2 implements the new Worker validation revision and clean/existing four-gate coverage. Task 3 corrects the `60229b5` raw bytes/provenance/report status regression while preserving UUID-only identity. Tasks 4–6 supersede original Tasks 10–12. Task 7 supersedes the original Task 13 release gate.
- **Placeholder scan:** Every implementation step includes concrete files, interfaces, commands, expected outcomes, and code or SQL where a change is required; no deferred-work marker remains.
- **Type consistency:** `EvidenceReference(id: UUID)` and the structured-data-only `PersistedEvidence` application shape are unchanged across Tasks 2, 3, and 7. Task 3 persists exact `raw_result: bytes` and provenance `metadata: dict[str, Any]` at the repository boundary without returning them to agent context; `result_status: Literal["COMPLETE", "PARTIAL", "FAILED"]` is stored before its smoke assertions. Worker revision name is consistently `0003_validate_ordinary_runtime_tables`.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-28-immutable-migration-rollout-correction.md`. Execute it task-by-task with independent reviewer gates; do not start a later task until its predecessor’s commit and reviewer decision are recorded.
