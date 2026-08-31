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

Tasks 1–7 已完成，Task 7 final release gate attempt 14 在 implementation HEAD
`072e5e3a7fad35d92fe4870dd5b03f9f60eceba1` 全部通過：

- 兩個 isolated disposable databases 均完成四個 explicit migration gates。
- Backend：354 passed；Worker：434 passed、1 skipped；contracts：60 passed。
- Backend／Worker Ruff clean；兩邊 Pyright 均為 0 errors、0 warnings。
- Compose、Kustomize、四個 migration Jobs 與 canonical evidence smoke 全部通過。
- Live flow 驗證 request-scoped publish、bounded publish failure、no automatic replay、protected manual recovery、terminal Worker processing 與 at-least-once no-duplicate。
- Catalog 為六張 canonical `r/f` 與六張 retained legacy parent `p/f`，總數 12。
- Mandatory cleanup 完成：兩個 release databases 已移除，只剩 shared `sre_agent|16384`；port 8000 clear；原 Pub/Sub emulator identity、binding、label 與 health 不變。
- 完整證據記錄於 `.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-7-report.md` 的 attempt 14 `READY_FOR_FINAL_REVIEW` 段落。
- Final independent branch/evidence review：**APPROVED，無 Critical／Important finding**。

## Superpowers Plan

目前依據：

- 已核准 spec：`docs/superpowers/specs/2026-08-26-backend-runtime-simplification-design.md`
- 目前 implementation plan：`docs/superpowers/plans/2026-08-28-immutable-migration-rollout-correction.md`
- SDD ledger：`.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/progress.md`
- Task 6 round-4 fix brief：`.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-6-round-4-brief.md`
- Pub/Sub emulator transport defect brief：`.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-4-emulator-transport-fix-brief.md`
- Task 7 rerun brief：`.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-7-rerun-brief.md`

`docs/superpowers/plans/2026-08-26-backend-runtime-simplification-implementation.md` 是較早的 implementation plan；目前 correction plan 已接管，不要回到舊計畫。

目前執行位置：**Tasks 1–7 與 final independent review 已完成；待 completion checkpoint commit 與 branch finishing handoff。**

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

沒有 implementation task 仍在進行。只剩 completion checkpoint commit 與 branch finishing handoff。

## Next Action

1. 執行精簡 completion verification。
2. Commit plan checkbox 與本 handoff completion checkpoint；不要 push。
3. 依 `superpowers:finishing-a-development-branch` 交付 branch integration 選項。

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

本 completion checkpoint 只修改：

- `docs/superpowers/plans/2026-08-28-immutable-migration-rollout-correction.md`：Tasks 1–7 全部標記完成。
- `docs/superpowers/handoffs/CURRENT.md`：記錄 attempt 14 release evidence 與 final review 狀態。

Task 7 前置 defect-fix commits：

- `f2181de`：bounded Pub/Sub publish retries。
- `7777fe6`：修正 retained top-level partition parent catalog gate。
- `68e28b3`：修正 bounded shutdown boundary。
- `072e5e3`：調整 recovery shutdown ordering。

## Git State

- Worktree：`/Users/linyungyuan/Desktop/sre-agent2.0/.worktrees/backend-runtime-simplification`
- Branch：`codex/backend-runtime-simplification`
- Task 7 verified implementation HEAD：`072e5e3a7fad35d92fe4870dd5b03f9f60eceba1`（`docs: reorder task 7 recovery shutdown`）
- Current HEAD：completion checkpoint 提交後請以 `git rev-parse HEAD` 取得。
- Staged files：無
- Unstaged tracked files：無
- Untracked files：無

最近相關 commits（新到舊）：

```text
072e5e3 docs: reorder task 7 recovery shutdown
68e28b3 docs: fix task 7 shutdown boundary
7777fe6 docs: correct task 7 catalog gate
f2181de fix: bound pubsub publish retries
07b22be fix: preserve backend cleanup errors
077ed0b fix: preserve pubsub cleanup failures
e5b9c8e fix: close pubsub emulator transports
a37c1d4 fix: use insecure pubsub emulator transports
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

### Task 7 final release gate attempt 14

- Unified verification session：exit 0；完整 log：`/tmp/task7-attempt14.log`。
- 兩個 disposable DB 四 gate migration：全部通過。
- Backend：**354 passed, 40 warnings**。
- Worker：**434 passed, 1 skipped, 62 warnings**。
- Contracts：**60 passed**。
- Backend／Worker Ruff：clean；Backend／Worker Pyright：0 errors, 0 warnings。
- Compose／Kustomize／four Jobs／canonical evidence smoke：全部通過。
- Live request、failure、no-replay、manual recovery、terminal processing、no-duplicate：全部通過。
- Catalog：六 canonical `r/f` + 六 retained legacy parents `p/f`，總數 12。
- Cleanup：release DBs 不存在；shared `sre_agent|16384`；port 8000 clear；Pub/Sub emulator identity 與 health 保持正確。

## Known Issues

- Full Backend 有 40 個 Alembic `path_separator` deprecation warnings；是技術債，不是 failure。
- 四 gate fixture 的獨立 report 缺失（minor process debt）。
- 本地 branch 比 upstream 多若干 commits；尚未 push（依使用者要求）。
- Attempt 14 第一筆 live tuple UUID 已在 verification shell 中捕捉並以關聯 assertions 驗證，但未輸出到 log；第二筆 recovery tuple UUID 完整記錄。Independent review 判定這只影響證據可追溯性，不是 release blocker。

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
- 不要重跑已完成的完整 Task 7 gate，除非 final review 發現會影響 release 判定的實質證據缺口。
- 不要 stage、commit 或 push scope 外檔案；未獲使用者要求前不要 push。

## Resume Instructions

下一個 AI 應先讀取本檔、active spec、correction plan、`progress.md` 與 `task-7-report.md`。

使用 Superpowers 的順序：

1. `superpowers:using-superpowers`：恢復 skill discipline。
2. `superpowers:verification-before-completion`：執行精簡 completion checks。
3. `superpowers:finishing-a-development-branch`：交付已驗證 branch；不要 push。

本階段不需要 `superpowers:brainstorming`：architecture 與操作政策已核准，下一步是完成既有 verification，不是重新設計。
