# SRE Agent 專案交接

這是給下一個 Codex／開發者的快速接手文件。穩定且長期適用的規則放在根目錄
`AGENTS.md`；本文件記錄建立當下的進度與風險。開始工作前仍須以 Git、測試結果與
權威手冊重新確認現況。

## 交接快照

- 快照日期：2026-08-31
- 基準 branch：`main`
- 基準 commit：`68cd7ca`（Merge pull request #13）
- 建立本文件的工作 branch：`codex/add-project-handoff-docs`
- 三個部署服務：Backend、RCA Worker、Frontend
- 指定的下一項產品功能：無；開始新功能前先向使用者確認範圍

若此資訊與目前 repository 不同，以以下命令的結果為準：

```bash
git status --short --branch
git log --oneline --decorate -10
git fetch origin
git rev-parse HEAD origin/main
```

## 已完成的重要變更

### PR #11：Backend runtime 簡化

- 移除獨立 outbox polling worker 與 partition maintenance runtime。
- Backend 在 request transaction commit 後發布 Pub/Sub event。
- Outbox backlog 只能透過受保護的人工 recovery API 重試，不會在 startup、排程或新
  request 中自動重播。
- Backend 與 Worker 的設定、程式碼與資料庫所有權邊界已進一步隔離。

### PR #12：Migration 維運入口

- 保留 Backend-0002、Worker-0002、Backend-0003、Worker-0003 四個獨立 Kubernetes Job。
- 新增 `deploy/k8s/run-migrations.sh`，讓操作者以一次指令依序建立並等待四個 Job。
- 任一 Job 失敗時立即停止，輸出該 Job 的 describe 與 log，不建立後續 Job。
- GKE runbook 與契約測試已同步更新。

### PR #13：資料庫文件中文化

- `docs/database/postgresql-schema.md` 的說明文字已改為繁體中文。
- SQL、Alembic 指令、identifier 與四個 migration gate 順序保持不變。
- 文件契約測試已改用中文標題與安全敘述。
- 合併前相容性測試結果為 `62 passed`，GitHub check 為 1/1 通過。

## 目前架構摘要

- `backend/`：FastAPI webhook 與 Operator API，擁有 ingestion、incident、outbox、
  audit 等 Backend schema；正式 container 使用 `backend/Dockerfile`。
- `rca-worker/`：Pub/Sub consumer 與 ADK/MCP RCA orchestration，擁有 run、evidence、
  report、job 等 Worker schema；正式 container 使用 `rca-worker/Dockerfile`。
- `frontend/`：Angular 22 操作介面，只呼叫 REST API；正式 container 使用
  `frontend/Dockerfile`。
- `contracts/`：三個服務共用的版本化格式與相容性測試，不部署成服務。
- `deploy/k8s/`：GKE application base、四個 migration Job 與發布 runner。

完整說明請先閱讀 `README.md` 與 `AGENTS.md`，再依任務閱讀各服務 README。架構圖入口：

- `docs/architecture/sre-agent-whole-project-architecture.html`：整體專案架構。
- `docs/architecture/sre-agent-end-to-end-dataflow.html`：端到端資料流。
- `docs/architecture/sre-agent-architecture.html`：系統架構總覽。
- `docs/diagrams/rca-worker-architecture-flow.html`：RCA Worker 完整架構與流程。
- `docs/diagrams/rca-worker-adk-specialist-architecture-flow.html`：ADK Specialist 流程。

## Migration 與部署現況

資料庫固定執行順序：

1. Backend `0002_grafana_normalization_v2`
2. Worker `0002_adk_specialist_analysis`
3. Backend `0003_non_partition_runtime_tables`
4. Worker `0003_validate_ordinary_runtime_tables`

GKE 操作者從發布 bundle 根目錄執行：

```bash
deploy/k8s/run-migrations.sh
kubectl apply -k deploy/k8s/base
kubectl rollout restart deployment/sre-agent-backend
kubectl rollout restart deployment/sre-agent-frontend
kubectl rollout restart deployment/sre-agent-rca-worker
```

實際發布前必須依 `deploy/k8s/README.md` 準備不可變 image digest、namespace、Secret、
Workload Identity、Cloud SQL、Pub/Sub 與 Gateway。資料庫備份、每個 gate 的版本表檢查、
失敗與復原條件以 `docs/database/postgresql-schema.md` 為準。

## 已知限制與風險

- 正式環境 Operator API 身分驗證尚未實作；`APP_ENVIRONMENT=production` 時會以
  HTTP 503 fail closed。不得用 `local` 模式繞過。
- RCA Specialist rollout 預設為 `SPECIALIST_ANALYSIS_MODE=DISABLED`。必須依
  `rca-worker/README.md` 完成 DISABLED → SHADOW → ACTIVE 的 audit gate，不得因部署新版
  自動啟用 ACTIVE。
- MCP capability 缺失、不安全或 scope 不足時，Worker 必須跳過不安全呼叫並產生
  `PARTIAL`，不能讓模型自行改 endpoint、tool、scope 或 query。
- Database migration 是 forward-only。新版本開始寫入後，不得依賴 Alembic downgrade
  回復舊狀態。
- Repository 不管理 Cloud SQL、Pub/Sub、Gateway、DNS、TLS、Load Balancer 或
  Workload Identity 等環境資源；部署前必須由外部基礎設施流程提供。
- Frontend 目前只在使用者按「重新整理」時重新讀取資料；沒有 timer、SSE、WebSocket
  或 Chat。

## 本機工作目錄注意事項

`docs/architecture/` 與 `docs/diagrams/` 已納入版本控制。架構或流程改變時，需同步更新
對應 HTML、來源 JSON 與 visual-check 驗證紀錄，並確認圖中的服務邊界、migration 順序
及部署流程仍與權威文件一致。

其他 worktree／branch 也可能存在。不要因看起來過時就刪除；先確認 ownership 與
使用者意圖。

## 下一個 Codex 的啟動清單

1. 從 repository root 開啟專案，確認 `main` 已同步 `origin/main`。
2. 閱讀 `AGENTS.md`、`README.md`、本文件，以及任務涉及的服務 README。
3. 執行 `git status --short --branch`，辨識使用者既有的 modified／untracked files。
4. 用 `git log --oneline --decorate -10` 與 GitHub PR 狀態核對最新進度。
5. 先用繁體中文摘要理解到的架構、資料庫順序、部署流程與任務範圍，再修改檔案。
6. 只建立本次任務需要的 `codex/` branch；保留使用者與其他 worktree 的內容。
7. 修改後執行受影響的服務測試、契約測試與 `git diff --check`，並清楚回報未跑項目。

## 驗證入口

```bash
make test-contracts
make test-backend
make test-rca-worker
make test-frontend
make check
```

完整本機 runtime 啟動、Grafana webhook smoke test 與停止方式請依 `README.md`；不要在
未確認資料庫、Pub/Sub Emulator、Node 版本與必要 credentials 前直接執行完整流程。
