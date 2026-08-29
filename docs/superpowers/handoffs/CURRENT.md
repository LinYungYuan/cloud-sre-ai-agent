# AI Development Handoff

## Goal

完成 Backend runtime simplification 與 immutable migration rollout correction：

- Backend request transaction 同時保存業務資料、RCA run、worker job 與 durable outbox event；commit 後只發布本次 request 建立的 event。
- 移除獨立 Outbox Worker 與 Partition Worker；歷史 `PENDING`／`FAILED` event 只經受保護的 REST API 人工重送，不在 Backend startup 或背景排程自動 replay。
- 將六張 canonical runtime tables 轉為普通、UUID-primary-key PostgreSQL tables，並保留六張 `__partitioned_legacy_0003` legacy partition parents。
- Backend、RCA Worker 與兩組 migration 各自使用隔離環境設定；AI／MCP 參數只屬於 RCA Worker。
- 依 Backend-0002 → Worker-0002 → Backend-0003 → Worker-0003 四個明確 migration gates 完成 release acceptance，不跨 gate 使用 `upgrade head` 或 `stamp`。
- 保留 evidence 的原始 bytes、metadata、content hash 與 report `result_status`，同時維持 UUID-only references，且不把 raw payload 載入 AI context。

實際開發 worktree：

`/Users/linyungyuan/Desktop/sre-agent2.0/.worktrees/backend-runtime-simplification`

不要在 `/Users/linyungyuan/Desktop/sre-agent2.0` 的 `main` checkout 接續本工作；該 checkout 另有未追蹤的 `docs/architecture/` 與 `docs/diagrams/`，不屬於本分支交接範圍。

## Current State

整體實作已進入最後文件修正與 release gate 階段，但尚未完成 release acceptance：

- Tasks 1–5 已完成其實作與 scoped independent review。
- Task 6 的主要 rollout/runbook 修正已提交於 `b5f20e6`，schema evolution reference 修正已提交於 `51ee41b`。
- Task 6 round-2 independent review 發現 Backend-0001 與 Backend-0002 Incident 欄位混在同一歷史區段，因此要求 round 3。
- Task 6 round 3 已提交為 `4ca879c`，focused test 與本次 handoff 的 scoped checks 通過；但尚未 independent re-review，因此不得宣稱 Task 6 完成。
- Task 7 的舊成功報告早於 `83062de`／`b5f20e6`，僅是歷史證據。post-fix attempt 1 在 Backend schema-documentation tests 失敗後依規定停止。修正 Task 6 並通過 re-review 後，Task 7 必須從 Step 1 全新重跑。

目前沒有正在執行的實作 sub-agent。先前 Task 7 attempt 1 已清除 `sre_agent_release_acceptance` 與 `sre_agent_release_tests`；報告記錄 shared `sre_agent` OID 仍為 `16384`。既有 PostgreSQL 與 Pub/Sub containers 當時保留運行，下一次 Task 7 必須重新做 safety preflight，不能把此歷史狀態當成即時保證。

## Superpowers Plan

目前依據：

- 已核准 spec：`docs/superpowers/specs/2026-08-26-backend-runtime-simplification-design.md`
- 目前 implementation plan：`docs/superpowers/plans/2026-08-28-immutable-migration-rollout-correction.md`
- SDD ledger：`.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/progress.md`
- Task 6 fix brief：`.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-6-fix-brief.md`
- Task 6 round-2 review：`.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-6-review-round-2.md`
- Task 7 rerun brief：`.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-7-rerun-brief.md`

`docs/superpowers/plans/2026-08-26-backend-runtime-simplification-implementation.md` 是較早的 implementation plan；目前 correction plan 已接管 migration correction、runtime removal、deployment、documentation 與 final release gate，不要回到舊計畫重新執行 Tasks 1–13。

目前執行位置：**Task 6, round 3，修改與 scoped verification 已提交為 `4ca879c`，等待 independent re-review；Task 7 尚未重新開始。**

## Completed

- Task 1：修正 Backend `0003_non_partition_runtime_tables`，保留 Worker-0002 evidence/lifecycle contract；`599a9dc` 與 static-analysis cleanup `bf1ac45` 已通過 scoped re-review。Backend-0003 自此凍結。
- Task 2：新增 validation-only Worker `0003_validate_ordinary_runtime_tables`；`b65b386`／`4c3391e` 已通過 scoped re-review。
- Task 3：恢復 exact `raw_result` bytes、metadata、hash、report status，保留 UUID-only evidence references；`9826c1e`／`3e21f33` 已通過 independent review。
- Task 4：移除 Outbox 與 Partition runtimes，保留 request-scoped publish 與 manual recovery；`93aefb9`／`0d50f8a` 已通過 independent review。
- 四 gate disposable fixture reconciliation：`d7a90cb` 已獨立審查通過；缺少獨立 fixture report 是 minor process debt。
- Task 5：Compose default ports 改為 PostgreSQL `5432`、Pub/Sub `58085`，Kubernetes 改為四個 exact-target migration Jobs；`4738f88`／`83062de`，並在 Task 6 contracts 修正後完成 scoped approval。
- Task 6 round 1：`b5f20e6` 建立 operator-safe 四 gate rollout、manual recovery/no-auto-replay 與 final schema contracts，round-1 scoped review approved。
- Task 6 round 2：`51ee41b` 補回完整 schema evolution reference，關閉 Task 7 attempt 1 的六個 schema-documentation failures；round-2 review 仍提出一個 Important 歷史區段矛盾，因此未核准整個 Task 6。

## In Progress

### Task 6 — round 3

round-2 review 指出 `docs/database/postgresql-schema.md` 的 Backend-0001 Incident definition 已含 Backend-0002 才新增／放寬的欄位，之後又以 0002 `ALTER` 重複描述。

round-3 commit `4ca879c` 包含：

- `docs/database/postgresql-schema.md`
  - 新增 `### Backend-0001 published baseline` 與 `### Backend-0002 normalization and identity mutations` 邊界。
  - 將 0001 的 `team_id`、`project_id`、`environment_id` 恢復為 `NOT NULL`。
  - 從 0001 移除 `identity_version`、`provider`、`folder_code`、`alert_name`。
  - 在 0002 區段完整記錄四個 `ADD COLUMN` 與三個 `DROP NOT NULL`。
  - final runtime section 保留 post-0002 Incident state 說明。
- `backend/tests/unit/persistence/test_schema_documentation.py`
  - 新增 `_section_between()`。
  - 新增 `test_schema_reference_separates_0001_baseline_from_0002_incident_mutations()`，以 section-bounded assertions 防止歷史階段再次混合。
- `.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-6-report.md`
  - 新增 round-3 trigger、RED/GREEN 與修改範圍紀錄。

狀態：修改已提交但尚未 independent re-review。不要把 commit 或報告中的 `READY_TO_COMMIT` 誤讀為 Task 6 或 release 已完成。

## Next Action

下一個 AI 的第一件事不是新增功能，而是完成 Task 6 round-3 的 independent re-review：

1. 在 `/Users/linyungyuan/Desktop/sre-agent2.0/.worktrees/backend-runtime-simplification` review `51ee41b..4ca879c`，不要修改其他檔案。
2. 逐段比對：
   - `docs/database/postgresql-schema.md` 的 `Backend-0001 published baseline` 應精確符合 `backend/migrations/versions/0001_alert_incident_schema.py` 的 `incidents` 定義。
   - `Backend-0002 normalization and identity mutations` 應精確符合 `backend/migrations/versions/0002_grafana_normalization_v2.py` 的 Incident mutations。
3. 檢查 `backend/tests/unit/persistence/test_schema_documentation.py` 中 `_section_between()` 與 `test_schema_reference_separates_0001_baseline_from_0002_incident_mutations()`；預期不放寬舊測試，只新增 section-bounded regression。
4. 執行：

   ```bash
   cd backend
   uv run pytest tests/unit/persistence/test_schema_documentation.py -q
   uv run ruff check tests/unit/persistence/test_schema_documentation.py
   cd ..
   git diff --check
   ```

5. 針對 `task-6-review-round-2.md` 的唯一 Important finding執行 independent re-review，並把 verdict 寫入新的 Task 6 round-3 review record。
6. 只有 re-review APPROVED 後，才依 `task-7-rerun-brief.md` 從 safety preflight／Step 1 全新重跑 Task 7；不可從 post-fix attempt 1 中途續跑。

## Important Decisions

- Backend commit 後只發布本次 request 的 event UUID，不掃描歷史 backlog。
- Backend restart、startup 與正常 request flow 都不自動重送 `PENDING`／`FAILED`；只使用 protected manual recovery REST API，不建立 recovery CLI。
- Durable `outbox_events` 保留；publish 失敗不回滾已 commit 的 webhook/RCA job，Grafana request 仍回 `202`。
- 移除獨立 Outbox Worker 與 Partition Worker；canonical runtime tables 使用普通非分區表。
- 六張 legacy partition parents 在初次 migration 保留；不宣稱新寫入後 downgrade 可無損完成。
- 五組環境檔互相隔離；Backend 不得持有 `MODEL_NAME`、Log/Metrics/Trace MCP URLs 或 specialist/evidence settings，這些只屬於 RCA Worker。
- Migration 固定四 gate：Backend `0002_grafana_normalization_v2` → Worker `0002_adk_specialist_analysis` → Backend `0003_non_partition_runtime_tables` → Worker `0003_validate_ordinary_runtime_tables`。
- Worker `0001_rca_worker_v1`、Worker `0002_adk_specialist_analysis` 永久 immutable；不得修改、squash、stamp 或 replay。
- Backend `0003_non_partition_runtime_tables` 已在 Task 1 凍結；後續缺陷必須新增 forward revision，不能原地修改。
- Evidence 儲存 exact raw bytes／metadata／content hash，但 `PersistedEvidence` 與 AI context 維持 bounded structured data；引用只使用 UUID。
- Task 7 使用固定 disposable DB names，若任一已存在就 abort；shared `sre_agent` 永遠不得 migrate、truncate 或 drop。

## Files Changed

最新 implementation commit `4ca879c` 修改：

- `.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-6-report.md`：記錄 Task 6 round-3 原因、TDD 與驗證證據。
- `backend/tests/unit/persistence/test_schema_documentation.py`：新增歷史 schema section boundary regression。
- `docs/database/postgresql-schema.md`：分離 Backend-0001 baseline 與 Backend-0002 mutations。

後續 docs-only handoff commit 新增：

- `docs/superpowers/handoffs/CURRENT.md`：本交接文件；不屬於 implementation 修改。

## Git State

- Worktree：`/Users/linyungyuan/Desktop/sre-agent2.0/.worktrees/backend-runtime-simplification`
- Branch：`codex/backend-runtime-simplification`
- Implementation HEAD：`4ca879c7f1455bac65ed5f8f16fbf158d10d8022` (`docs: separate published schema evolution stages`)
- Current HEAD：本檔由下一個 docs-only commit 提交，因此請以 `git rev-parse HEAD` 取得該 handoff commit；implementation baseline 固定為上列 `4ca879c`。
- Upstream：`origin/codex/backend-runtime-simplification`
- Ahead after handoff commit：5 commits (`83062de`, `b5f20e6`, `51ee41b`, `4ca879c`，以及本 handoff docs commit)
- Staged files：無
- Unstaged tracked files：無
- Untracked files：無

最近相關 commits（新到舊）：

```text
4ca879c docs: separate published schema evolution stages
51ee41b docs: restore complete schema evolution reference
b5f20e6 docs: enforce operator-safe four-gate rollout
83062de fix: enforce explicit deployment gates
136d3f9 fix: update evidence insert fixture in worker tests
9ded769 docs: document immutable migration rollout
4738f88 refactor: simplify runtime deployments
d7a90cb test: reconcile disposable databases with four gates
3e21f33 fix: complete exact uuid-only worker evidence audit
0d50f8a docs: record obsolete runtime removal evidence
93aefb9 refactor: remove outbox and partition workers
9826c1e fix: preserve exact uuid-only worker evidence
4c3391e fix: qualify worker gate and prove clean catalog
b65b386 feat: validate worker ordinary-table migration gate
bf1ac45 fix: satisfy backend static analysis
599a9dc fix: preserve worker evidence in backend migration
```

不要 push；使用者只要求 session handoff。

## Verification

### 本次 handoff fresh scoped verification

- `cd backend && uv run pytest tests/unit/persistence/test_schema_documentation.py -q`
  - 結果：`10 passed in 0.86s`。
- `cd backend && uv run ruff check tests/unit/persistence/test_schema_documentation.py`
  - 第一次因 sandbox 無權讀取既有 `~/.cache/uv` 而未執行；經授權重跑後結果為 `All checks passed!`。
- `git diff --check`
  - exit code `0`，無輸出。

### Task 6 round-3 report 中的先前驗證紀錄

以下是 implementer 在本次 handoff 前記錄的結果，未在本次 handoff 全量重跑：

- TDD RED：新 section-bounded regression 在加入正確文件邊界前為 1 fail。
- Focused schema documentation：10 passed。
- Full Backend：345 passed，40 warnings。
- Full compatibility contracts：56 passed。
- Backend／Worker Ruff：passed。
- Backend／Worker Pyright：0 errors、0 warnings。
- Operator stale scan：無 unsafe matches。
- Immutable migration path diff：empty。
- `git diff --check`：clean。

### Task 7 狀態

- 舊的 827-test、live smoke 與 catalog success 是 `83062de`／`b5f20e6` 之前的歷史結果，不可作為目前 release acceptance。
- post-fix attempt 1 的四個 explicit migration gates 在兩個 disposable DB 都成功，但 Backend full suite 為 `338 passed, 6 failed, 40 warnings`，所以其餘 Worker/contracts/static/render/canonical/live gates均未執行。
- attempt 1 的兩個 disposable DB 已清理；Task 7 必須在 Task 6 re-review 後從頭重跑。

## Known Issues

- Task 6 round 3 已提交但尚未 independent re-review；這是目前最直接 blocker。
- Task 7 post-fix full release gate 尚未完成，不能宣稱 release ready。
- `.superpowers/.../progress.md` 內仍保留曾經「Task 7 complete」的歷史段落，但後續 reopening 條目已明確推翻該狀態；以最新 reopening／attempt 1／本 handoff 為準。
- Full Backend 先前有 40 個 Alembic `path_separator` deprecation warnings；不是本輪 failure，但仍是技術債。
- 四 gate fixture 的獨立 report 缺失，review 將其列為 minor process debt。
- handoff commit 完成後，本地 branch 比 upstream 多 5 commits；尚未 push。
- 下一次 Task 7 開始前必須重新確認 ports、container ownership、disposable DB absence、shared DB OID 與無殘留 processes；不能沿用舊報告的 runtime 狀態。

## Do Not Do

- 不要重新設計 architecture、重新 brainstorming，或任意推翻上方 Important Decisions。
- 不要重新實作 Tasks 1–5。
- 不要修改 published Worker `0001`／`0002` migrations。
- 不要修改已凍結的 Backend `0003_non_partition_runtime_tables`。
- 不要以 `alembic upgrade head`、`stamp` 或 replay immutable Worker migrations 跨 gate。
- 不要 migrate、truncate、drop 或用作 release test 的 shared `sre_agent` database。
- 不要刪除 retained legacy partition parents。
- 不要恢復 Outbox Worker、Partition Worker、automatic startup replay 或 recovery CLI。
- 不要把 AI／MCP settings 加回 Backend。
- 不要把 raw evidence payload 加入 `PersistedEvidence` 或 AI context。
- 不要放寬或刪除 schema-documentation tests 來讓文件通過。
- 不要重寫或回退 `4ca879c` 的三個 Task 6 round-3 修改，也不要碰 `main` checkout 的未追蹤文件。
- 不要 stage、commit 或 push scope 外檔案；未獲使用者要求前不要 push。

## Resume Instructions

下一個 AI 應先讀取本檔、active spec、correction plan、`progress.md`、`task-6-report.md`、`task-6-review-round-2.md` 與 `task-7-rerun-brief.md`。

使用 Superpowers 的順序：

1. `superpowers:using-superpowers`：恢復 skill discipline。
2. `superpowers:receiving-code-review`：把目前 round-3 diff 視為對既有 Important review finding 的修正，逐項核對，而不是盲目接受 implementer 報告。
3. `superpowers:verification-before-completion`：在 commit/re-review 前重新取得 focused evidence。
4. 若 round-3 commit 通過 independent review，再使用 `superpowers:executing-plans` 或 `superpowers:subagent-driven-development`，嚴格依 `task-7-rerun-brief.md` 從 Step 1 執行 Task 7。
5. Task 7 全部 fresh gates 通過後，才使用 `superpowers:requesting-code-review`／`superpowers:verification-before-completion` 做 final review；未全部通過不得宣稱完成。

本階段不需要 `superpowers:brainstorming`：architecture 與操作政策已核准，下一步是完成既有 review fix 與驗證，不是重新設計。
