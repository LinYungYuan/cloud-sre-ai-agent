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

整體實作已進入最後 release gate 階段：

- Tasks 1–6 全部完成；Task 6 round 4 文件修正與 Task 4 Pub/Sub emulator transport follow-up 均已通過 independent review。
- Task 7 attempt 7 fresh 通過 migrations、完整 tests/static/renders/evidence、catalog seed 與第一筆 live 202/PUBLISHED/SUCCEEDED；第二個既有 AWS fixture 因與第一筆共用 source + folder + alert name identity 而沒有建立新 outbox/run/job，故在 recovery 前正確停止。
- Task 7 procedure 已改為記憶體內產生 distinct recovery alert，並補齊 exact container ownership、fail-closed phase dispatcher、delivery-linked assertions、direct runtime PID ownership 與 bounded INT→TERM cleanup；final independent review APPROVED。
- Attempt 7 的兩個 disposable DBs 與 runtimes 已清理；下一次必須從 Safety Preflight 全新重跑，不可沿用任何部分結果。

## Superpowers Plan

目前依據：

- 已核准 spec：`docs/superpowers/specs/2026-08-26-backend-runtime-simplification-design.md`
- 目前 implementation plan：`docs/superpowers/plans/2026-08-28-immutable-migration-rollout-correction.md`
- SDD ledger：`.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/progress.md`
- Task 6 round-4 fix brief：`.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-6-round-4-brief.md`
- Pub/Sub emulator transport defect brief：`.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-4-emulator-transport-fix-brief.md`
- Task 7 rerun brief：`.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-7-rerun-brief.md`

`docs/superpowers/plans/2026-08-26-backend-runtime-simplification-implementation.md` 是較早的 implementation plan；目前 correction plan 已接管，不要回到舊計畫。

目前執行位置：**Task 7 final fresh rerun；完整 fail-closed procedure 已核准，所有 disposable DBs 已清理，必須從 Safety Preflight 全新重跑。**

## Completed

- Task 1：修正 Backend `0003_non_partition_runtime_tables`；`599a9dc` + `bf1ac45` 已通過 scoped re-review。Backend-0003 已凍結。
- Task 2：新增 validation-only Worker `0003_validate_ordinary_runtime_tables`；`b65b386`／`4c3391e` 已通過 scoped re-review。
- Task 3：恢復 exact `raw_result` bytes、metadata、hash、report status，保留 UUID-only evidence references；`9826c1e`／`3e21f33` 已通過 independent review。
- Task 4：移除 Outbox 與 Partition runtimes，保留 request-scoped publish 與 manual recovery；`93aefb9`／`0d50f8a` 已通過 independent review。
- 四 gate disposable fixture reconciliation：`d7a90cb` 已獨立審查通過。
- Task 5：Compose default ports 改為 PostgreSQL `5432`、Pub/Sub `58085`，Kubernetes 改為四個 exact-target migration Jobs；`4738f88`／`83062de`，並完成 scoped approval。
- Task 6 round 1：`b5f20e6`，scoped review approved。
- Task 6 round 2：`51ee41b`，修補 schema documentation，round-2 review 仍有 Important finding 未解。
- Task 6 round 3：`4ca879c`，分離 Backend-0001/0002 schema evolution 區段，新增 section-bounded regression test；**本 handoff session 執行 independent re-review：APPROVED（無 Critical/Important 發現）**。
- Task 6 round 4：`c316537` 恢復 `Backend migration → RCA Worker migration` 與 `無法還原` 文件契約；Worker focused 1 passed、Backend schema documentation 10 passed、contracts 56 passed，independent review 無任何 finding。
- Task 4 Pub/Sub emulator follow-up：`a37c1d4` 顯式建立 insecure transports；`e5b9c8e`、`077ed0b`、`07b22be` 依三輪 scoped review 補齊 deterministic close、partial-construction cleanup、test doubles 與 primary-error preservation；final scoped review APPROVED。
- Task 7 Safety preflight（本 handoff session）：✅ worktree clean，migration hashes 已記錄，無 release DBs，OID=16384。
- Task 7 Step 1 四 gate migration 兩個 DB（本 handoff session）：
  - `sre_agent_release_acceptance`：Backend-0002 → Worker-0002 → Backend-0003 → Worker-0003，全部 ✅。
  - `sre_agent_release_tests`：Backend-0002 → Worker-0002 → Backend-0003 → Worker-0003，全部 ✅。
  - （兩個 DB 已在 handoff 前清理，需重建）
- Task 7 Step 2 Backend（本 handoff session）：**345 passed, 40 warnings**（Alembic `path_separator` deprecation warnings，已知技術債，不是 failure）。

## In Progress

### Task 7 — post-fix full release gate（attempt 7 被重複 incident identity 停止）

**attempt 7 fresh evidence（已清理，不可作為完成憑證）：**

- Safety preflight ✅
- Step 1：兩個 disposable DB 四 gate migration ✅（已清理）
- Step 2 Backend 352 passed ✅
- Step 2 Worker 434 passed、1 skipped ✅；contracts 56 passed ✅。
- Static、Compose/Kustomize/four Jobs、canonical evidence smoke ✅。
- Catalog seed/assertion ✅；第一筆 live 202/PUBLISHED/SUCCEEDED/PARTIAL ✅。
- 第二筆 fixture ❌：共用 incident identity，未建立新 outbox；procedure 已改為 in-memory distinct alert 並 final review APPROVED。

**尚未執行（因第二筆 request 未建立獨立 recovery work 而正確停止）：**

- Step 6：Live request/recovery smoke
- Step 7：Catalog 驗證（六 `r` + 六 `p`）
- Step 8：Cleanup（此次已清理，下次亦需清理）

**report 檔案目前狀態：** `.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-7-report.md` 已追加 attempt 7 的 NOT READY、identity blocker 與 cleanup 證據；下一次仍需以新的 fresh section 記錄完整 rerun。

## Next Action

下一個動作是**從頭重跑 Task 7 final fresh gate**，嚴格依已修正的 `task-7-rerun-brief.md` 與 tracked Task 7 functions：

### Step 0：Safety Preflight

```bash
cd /Users/linyungyuan/Desktop/sre-agent2.0/.worktrees/backend-runtime-simplification
git status --short  # 預期：空白（worktree clean）
md5 backend/migrations/versions/0001_alert_incident_schema.py   # 預期：207f8b7d579ca78464608682a2542829
md5 backend/migrations/versions/0002_grafana_normalization_v2.py # 預期：8d920ca8dd68ff94c1467d582e354106
md5 rca-worker/migrations/versions/0001_rca_worker_v1.py         # 預期：caf5d8b23a248baa200db0f1feb3663c
md5 rca-worker/migrations/versions/0002_adk_specialist_analysis.py # 預期：4e2c7f97afd8d37345fb295a7ada252f
docker exec sre-agent20-local-postgres psql -U postgres -c "\l" | grep sre_agent_release  # 預期：無輸出
docker exec sre-agent20-local-postgres psql -U postgres -d sre_agent -c "SELECT oid FROM pg_database WHERE datname='sre_agent';"  # 預期：16384
```

### Step 1：建立兩個 Disposable DB，執行四 gate migration

依 `task-7-rerun-brief.md` 的 Fresh Gate 1：建立 `sre_agent_release_acceptance` 與 `sre_agent_release_tests`，對每個依序執行四個 explicit revision（Backend-0002 → Worker-0002 → Backend-0003 → Worker-0003）。每個 gate 後查詢兩個 version tables。

### Step 2：完整測試套件（使用 sre_agent_release_tests）

```bash
# Backend（預期 345 passed）
cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest -q

# RCA Worker（完整）
cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent_release_tests UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest -q

# Contracts
UV_CACHE_DIR="$PWD/backend/.uv-cache" uv run --project backend pytest contracts/compatibility-tests -q
```

### Steps 3–8

依 `task-7-rerun-brief.md` 繼續執行靜態分析、Compose/K8s 驗證、canonical evidence smoke、live smoke、catalog 驗證、cleanup。

完成後追加新段落至 `task-7-report.md`，若全部通過回報 `READY_FOR_FINAL_REVIEW`。

## Important Decisions

- Backend commit 後只發布本次 request 的 event UUID，不掃描歷史 backlog。
- Backend restart、startup 與正常 request flow 都不自動重送 `PENDING`／`FAILED`；只使用 protected manual recovery REST API。
- Durable `outbox_events` 保留；publish 失敗不回滾已 commit 的 webhook/RCA job。
- 移除獨立 Outbox Worker 與 Partition Worker；canonical runtime tables 使用普通非分區表。
- 六張 legacy partition parents 在初次 migration 保留；不宣稱新寫入後 downgrade 可無損完成。
- 五組環境檔互相隔離；Backend 不得持有 AI/MCP 相關設定。
- Migration 固定四 gate：Backend `0002_grafana_normalization_v2` → Worker `0002_adk_specialist_analysis` → Backend `0003_non_partition_runtime_tables` → Worker `0003_validate_ordinary_runtime_tables`。
- Worker `0001`、Worker `0002` 永久 immutable；不得修改、squash、stamp 或 replay。
- Backend `0003_non_partition_runtime_tables` 已在 Task 1 凍結；後續缺陷必須新增 forward revision。
- Evidence 儲存 exact raw bytes／metadata／content hash，但 `PersistedEvidence` 與 AI context 維持 bounded structured data；引用只使用 UUID。
- Task 7 使用固定 disposable DB names（`sre_agent_release_acceptance`、`sre_agent_release_tests`）；若任一已存在就 abort；shared `sre_agent` 永遠不得 migrate、truncate 或 drop。

## Files Changed

本 handoff session 未修改任何 source code 或 migration 檔案。

最新 implementation commit `4ca879c` 修改（Task 6 round-3）：

- `.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-6-report.md`：記錄 Task 6 round-3 原因、TDD 與驗證證據。
- `backend/tests/unit/persistence/test_schema_documentation.py`：新增 `_section_between()` 與歷史 schema section boundary regression test。
- `docs/database/postgresql-schema.md`：分離 Backend-0001 baseline 與 Backend-0002 mutations（新增兩個 heading 邊界，修正 incidents 欄位，補全 0002 ADD COLUMN 清單）。

handoff commit 修改：

- `docs/superpowers/handoffs/CURRENT.md`：本交接文件。

## Git State

- Worktree：`/Users/linyungyuan/Desktop/sre-agent2.0/.worktrees/backend-runtime-simplification`
- Branch：`codex/backend-runtime-simplification`
- Implementation HEAD：`07b22bebd719529d83c9963d3dec911745039fd8`（`fix: preserve backend cleanup errors`）
- Current HEAD：本 handoff checkpoint 提交後請以 `git rev-parse HEAD` 取得。
- Staged files：無
- Unstaged tracked files：無
- Untracked files：無

最近相關 commits（新到舊）：

```text
07b22be fix: preserve backend cleanup errors
077ed0b fix: preserve pubsub cleanup failures
e5b9c8e fix: close pubsub emulator transports
a37c1d4 fix: use insecure pubsub emulator transports
fc15ca4 docs: checkpoint task 6 round 4
 c316537 docs: restore migration stream order contract
3b2cf9a docs: update handoff — task-6 round-3 approved, task-7 partial cleanup
8911467 docs: add current ai development handoff
4ca879c docs: separate published schema evolution stages
51ee41b docs: restore complete schema evolution reference
b5f20e6 docs: enforce operator-safe four-gate rollout
83062de fix: enforce explicit deployment gates
136d3f9 fix: update evidence insert fixture in worker tests
9ded769 docs: document immutable migration rollout
4738f88 refactor: simplify runtime deployments
d7a90cb test: reconcile disposable databases with four gates
3e21f33 fix: complete exact uuid-only worker evidence audit
```

不要 push；使用者只要求 session handoff。

## Verification

### Task 4 Pub/Sub emulator transport follow-up（本 session）

- Task 7 attempt 4 live RED：Worker 對 plaintext emulator 使用 TLS，`WRONG_VERSION_NUMBER`。
- TDD commits：`a37c1d4`、`e5b9c8e`、`077ed0b`、`07b22be`。
- Scoped re-review round 3：所有 findings addressed，無新 breakage，APPROVED。
- Controller fresh：Backend lifecycle `12 passed, 1 deselected`；Worker lifecycle＋identity integration `10 passed`；Backend/Worker Ruff clean；兩邊 Pyright `0 errors, 0 warnings`；`git diff --check` clean。

### Task 6 round-4 correction and independent review（本 session）

- Task 7 attempt 2 Worker full suite fresh failure：`1 failed, 429 passed, 1 skipped`，缺少 `Backend migration → RCA Worker migration`。
- Focused RED：Worker schema-documentation test `1 failed`。
- `c316537` GREEN：Worker focused `1 passed`、Backend schema-documentation `10 passed`、contracts `56 passed`、stale-text scan 與 `git diff --check` clean。
- Independent scoped review：Spec compliant、Task quality Approved；Critical／Important／Minor 均無。

### Task 6 round-3 independent re-review（本 handoff session）

本 session 執行了 Task 6 round-3 independent re-review（依 `CURRENT.md` Next Action 指示）：

- 對照 `task-6-review-round-2.md` 的唯一 Important finding（0001 baseline 含 0002-only 欄位）：**已解決**。
  - `4ca879c` 的 0001 區段精確符合 `0001_alert_incident_schema.py:184-186`（三個 NOT NULL scope columns）。
  - 0001 區段不含 `identity_version`、`provider`、`folder_code`、`alert_name`。
  - 0002 mutations 區段完整含四個 `ADD COLUMN` 與三個 `DROP NOT NULL`，符合 `0002_grafana_normalization_v2.py:121-128`。
  - Section-bounded regression test 通過。
- **Verdict：Task 6 round-3 APPROVED。**

Verification 結果（本 handoff session fresh 執行）：

- `pytest tests/unit/persistence/test_schema_documentation.py -q`：`10 passed`
- `ruff check tests/unit/persistence/test_schema_documentation.py`：`All checks passed!`
- `git diff --check`：clean（exit 0）

### Task 7 中途執行結果（本 handoff session，已清理）

- Safety preflight：✅ worktree clean，無 release DBs，OID=16384
- 不可變 migration MD5：
  - `0001_alert_incident_schema.py`：`207f8b7d579ca78464608682a2542829`
  - `0002_grafana_normalization_v2.py`：`8d920ca8dd68ff94c1467d582e354106`
  - `0001_rca_worker_v1.py`：`caf5d8b23a248baa200db0f1feb3663c`
  - `0002_adk_specialist_analysis.py`：`4e2c7f97afd8d37345fb295a7ada252f`
- 四 gate migration（acceptance DB）：每個 gate 的 version 查詢均符合預期。
- 四 gate migration（tests DB）：每個 gate 的 version 查詢均符合預期。
- Backend 完整測試：**345 passed, 40 warnings**（warnings 是已知 Alembic path_separator 技術債）。
- 兩個 disposable DBs 已在 handoff 前清理，shared `sre_agent` OID=16384 不變。

**以上結果不可作為 Task 7 完成的憑證；Task 7 必須全部 fresh gates 從 Step 1 重跑。**

## Known Issues

- Task 7 post-fix full release gate 尚未完成（不能宣稱 release ready）。
- Task 7 必須在確認 release DBs 不存在後從 Safety Preflight 全新重跑；attempt 2–7 的部分結果僅為歷史參考。
- Full Backend 有 40 個 Alembic `path_separator` deprecation warnings；是技術債，不是 failure。
- 四 gate fixture 的獨立 report 缺失（minor process debt）。
- `.superpowers/.../progress.md` 的 Task 7 段落有歷史「complete」記錄，但已被 reopening 條目明確推翻；以最新 reopening 與本 handoff 為準。
- 本地 branch 比 upstream 多若干 commits；尚未 push（依使用者要求）。
- `task-7-report.md` 已追加 attempt 2 的 NOT READY 與 cleanup 證據；尚無完整成功 rerun。
- 下一次 Task 7 開始前必須重新確認 ports、container ownership、disposable DB absence、shared DB OID 與無殘留 processes；不能沿用本 handoff 的 runtime 狀態。

## Do Not Do

- 不要重新設計 architecture、重新 brainstorming，或任意推翻上方 Important Decisions。
- 不要重新實作 Tasks 1–6。
- 不要修改 published Worker `0001`／`0002` migrations。
- 不要修改已凍結的 Backend `0003_non_partition_runtime_tables`。
- 不要以 `alembic upgrade head`、`stamp` 或 replay immutable Worker migrations 跨 gate。
- 不要 migrate、truncate、drop 或用作 release test 的 shared `sre_agent` database。
- 不要刪除 retained legacy partition parents。
- 不要恢復 Outbox Worker、Partition Worker、automatic startup replay 或 recovery CLI。
- 不要把 AI／MCP settings 加回 Backend。
- 不要把 raw evidence payload 加入 `PersistedEvidence` 或 AI context。
- 不要放寬或刪除 schema-documentation tests 來讓文件通過。
- 不要把本 handoff 的 Task 7 中途結果（Backend 345 passed）當作 Task 7 全部完成的憑證。
- 不要 stage、commit 或 push scope 外檔案；未獲使用者要求前不要 push。

## Resume Instructions

下一個 AI 應先讀取本檔、active spec、correction plan、`progress.md`、`task-6-report.md` 與 `task-7-rerun-brief.md`。

使用 Superpowers 的順序：

1. `superpowers:using-superpowers`：恢復 skill discipline。
2. `superpowers:verification-before-completion`：Task 7 全部 fresh gates 必須通過才可宣稱 Task 7 完成。
3. `superpowers:executing-plans` 或 `superpowers:subagent-driven-development`：嚴格依 `task-7-rerun-brief.md` 從 Step 1 全新執行 Task 7。
4. Task 7 全部 fresh gates 通過後，才使用 `superpowers:requesting-code-review`／`superpowers:verification-before-completion` 做 final review；未全部通過不得宣稱完成。

本階段不需要 `superpowers:brainstorming`：architecture 與操作政策已核准，下一步是完成既有 verification，不是重新設計。
