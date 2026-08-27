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

  Run each command separately. The two fixed names below are test-only targets; `createdb` must fail and the release gate must stop if either already exists. Never reuse, truncate, migrate, or drop the shared `sre_agent` database.

  ```bash
  docker compose --env-file .env.compose.example up -d postgres pubsub-emulator
  docker compose --env-file .env.compose.example exec -T postgres createdb -U postgres sre_agent_release_acceptance
  docker compose --env-file .env.compose.example exec -T postgres createdb -U postgres sre_agent_release_tests
  (cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade 0002_grafana_normalization_v2)
  (cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade 0002_adk_specialist_analysis)
  (cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade 0003_non_partition_runtime_tables)
  (cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade 0003_validate_ordinary_runtime_tables)
  ```

  After every migration command, query both version tables in `sre_agent_release_acceptance` and compare them with the expected current gate before continuing. Expected: all commands succeed; never replace a command with `upgrade head`.

- [ ] **Step 2: Run migration, Backend, Worker, and contract suites (2–5 minutes each command)**

  ```bash
  (cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade 0002_grafana_normalization_v2)
  (cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade 0002_adk_specialist_analysis)
  (cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade 0003_non_partition_runtime_tables)
  (cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade 0003_validate_ordinary_runtime_tables)
  (cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests uv run pytest tests -v)
  (cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests uv run pytest tests -v)
  uv run --project backend pytest contracts/compatibility-tests -v
  ```

  Expected: PASS. A test failure is routed to the owning task and that task repeats its own RED/GREEN evidence before this gate is restarted.

- [ ] **Step 3: Run full static checks (2–5 minutes each command)**

  ```bash
  (cd backend && uv run ruff check . && uv run pyright)
  (cd rca-worker && uv run ruff check . && uv run pyright)
  git diff --check
  ```

  Expected: zero Ruff and Pyright errors for both complete projects. A pre-existing error is not waived at this gate: route it to the owning task (or add a narrowly scoped defect task), fix it with RED/GREEN evidence, and restart the release gate.

- [ ] **Step 4: Run request/recovery and evidence end-to-end smoke (2–5 minutes per phase)**

  Start the processes in separate terminals with OS `DATABASE_URL` overriding only their own example file:

  ```bash
  (cd backend && DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance BACKEND_ENV_FILE=../.env.backend-api.example uv run uvicorn sre_agent.api.main:app --host 127.0.0.1 --port 8000)
  (cd rca-worker && DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_acceptance RCA_WORKER_ENV_FILE=../.env.rca-worker.example uv run sre-agent-rca-worker)
  ```

  Submit the first example and record the HTTP status/body:

  ```bash
  curl -fsS -X POST 'http://127.0.0.1:8000/webhooks/v1/grafana/50000000-0000-0000-0000-000000000001' -H 'Authorization: Bearer replace-me' -H 'Content-Type: application/json' --data-binary @contracts/examples/grafana-firing.json
  ```

  Expected: HTTP `202`; exactly one outbox event reaches `PUBLISHED`; Worker claims its job and persists a report with non-null `result_status`. Query `outbox_events`, `worker_jobs`, `rca_reports`, `evidence_records`, and `hypothesis_evidence` in `sre_agent_release_acceptance` for the returned delivery/run IDs, including byte-for-byte `raw_result`, provenance `metadata`, and UUID-only evidence links.

  Stop only `pubsub-emulator`, submit `contracts/examples/grafana-firing-aws.json`, and verify another HTTP `202` plus one `FAILED` outbox event. Stop Backend, restart the emulator and Worker (so local topic/subscription bootstrap runs), then restart Backend with the same command. Before recovery, query that the failed event is still `FAILED`; startup must not publish it. Invoke manual recovery:

  ```bash
  docker compose --env-file .env.compose.example stop pubsub-emulator
  curl -fsS -X POST 'http://127.0.0.1:8000/webhooks/v1/grafana/50000000-0000-0000-0000-000000000001' -H 'Authorization: Bearer replace-me' -H 'Content-Type: application/json' --data-binary @contracts/examples/grafana-firing-aws.json
  docker compose --env-file .env.compose.example up -d pubsub-emulator
  curl -fsS -X POST 'http://127.0.0.1:8000/api/v1/operations/outbox-events/retry-failed?limit=100' -H 'Authorization: Bearer local-operator'
  ```

  Expected: recovery returns a non-zero `selected`/`published` count without payload overrides; at-least-once processing produces no duplicate RCA run/job for an already accepted delivery.

- [ ] **Step 5: Capture catalog and rollback-policy evidence (2–5 minutes)**

  ```sql
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
  ) ORDER BY c.relname;
  ```

  Expected: six canonical `relkind='r'`/`relispartition=false` relations and six retained legacy partition parents. Record that no automatic downgrade or legacy cleanup has been executed.

- [ ] **Step 6: Clean disposable databases and commit only task-owned defect fixes (2–5 minutes)**

  First list the exact two test databases and confirm neither name is `sre_agent`, then remove only those two explicit targets:

  ```bash
  docker compose --env-file .env.compose.example exec -T postgres psql -U postgres -d postgres -c "SELECT datname FROM pg_database WHERE datname IN ('sre_agent_release_acceptance','sre_agent_release_tests') ORDER BY datname"
  docker compose --env-file .env.compose.example exec -T postgres dropdb -U postgres --force sre_agent_release_acceptance
  docker compose --env-file .env.compose.example exec -T postgres dropdb -U postgres --force sre_agent_release_tests
  ```

  If no defect files changed, do not create a verification-only source commit. If a defect is found, return it to its owning Task 1–6 file list, commit with that task’s message family, rerun its focused suite, then restart this release gate from Step 1. Store non-secret command output with release evidence outside the repository.

## Plan Self-Review

- **Spec coverage:** Task 1 implements true Worker 0002 preservation and fail-closed Backend conversion. Task 2 implements the new Worker validation revision and clean/existing four-gate coverage. Task 3 corrects the `60229b5` raw bytes/provenance/report status regression while preserving UUID-only identity. Tasks 4–6 supersede original Tasks 10–12. Task 7 supersedes the original Task 13 release gate.
- **Placeholder scan:** Every implementation step includes concrete files, interfaces, commands, expected outcomes, and code or SQL where a change is required; no deferred-work marker remains.
- **Type consistency:** `EvidenceReference(id: UUID)` and the structured-data-only `PersistedEvidence` application shape are unchanged across Tasks 2, 3, and 7. Task 3 persists exact `raw_result: bytes` and provenance `metadata: dict[str, Any]` at the repository boundary without returning them to agent context; `result_status: Literal["COMPLETE", "PARTIAL", "FAILED"]` is stored before its smoke assertions. Worker revision name is consistently `0003_validate_ordinary_runtime_tables`.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-28-immutable-migration-rollout-correction.md`. Execute it task-by-task with independent reviewer gates; do not start a later task until its predecessor’s commit and reviewer decision are recorded.
