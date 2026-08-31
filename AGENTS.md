# SRE Agent 專案指引

本文件提供所有在此 repository 工作的 Codex／開發者共用規則。開始修改前，先閱讀
根目錄 `README.md`，再依工作範圍閱讀對應服務文件。對使用者的說明預設使用繁體中文；
程式識別字、API path、環境變數、revision 與指令維持原文。

## 專案結構

此 monorepo 有三個可獨立建置與部署的服務：

| 目錄 | 責任 | Container |
| --- | --- | --- |
| `backend/` | FastAPI Grafana webhook 與 Operator REST API | `backend/Dockerfile`，port 8000 |
| `rca-worker/` | Pub/Sub 消費、ADK/MCP 編排、證據保存與 RCA 報告 | `rca-worker/Dockerfile` |
| `frontend/` | Angular 22 繁體中文操作介面 | `frontend/Dockerfile`，port 8080 |

`contracts/` 保存 OpenAPI、JSON Schema、範例、資料庫所有權契約與相容性測試，
不是第四個服務。`deploy/k8s/` 保存可攜式 GKE manifests 與發布 runner；雲端基礎設施
本身不在此 repository 的管理範圍。

## 必讀文件

- 整體架構、本機啟動與完整驗證：`README.md`
- Backend 設定與安全邊界：`backend/README.md`
- Worker、evidence-first 流程與 rollout gate：`rca-worker/README.md`
- Frontend runtime config 與操作範圍：`frontend/README.md`
- GKE 發布順序：`deploy/k8s/README.md`
- 資料庫與四個 migration gate：`docs/database/postgresql-schema.md`
- 最近完成事項與接手檢查：`docs/HANDOFF.md`

同一主題若有多份說明，以最接近實作的文件為準；資料庫操作以
`docs/database/postgresql-schema.md` 為權威，GKE 發布以 `deploy/k8s/README.md`
為權威。

## 架構邊界

- `backend/`、`rca-worker/` 與 `frontend/` 必須能獨立建置、測試與部署。
- Backend 與 Worker 不得匯入彼此的 `src`；只能共用 `contracts/` 中的版本化格式。
- Frontend 只透過 REST API 取得資料，不直接連線 PostgreSQL，也不匯入 Python source。
- Backend 擁有 webhook、Operator API、scope/source、delivery、alert、incident、timeline、
  outbox 與 audit 資料；Worker 擁有 run、specialist、evidence、hypothesis、report、job
  與 attempt 資料。機器可讀的所有權以 `contracts/database/table-ownership.yaml` 為準。
- Worker 的模型不能選擇 MCP endpoint、tool、scope、query 或 mutation；不安全或缺少
  capability 時必須 fail closed 或產生誠實的 `PARTIAL` 報告。
- 不得重新加入獨立 outbox polling worker、partition maintenance runtime，或自動重播
  `PENDING`／`FAILED` outbox backlog。

## 開發環境與常用命令

需求：Python 3.11 以上、`uv`、Node.js `>=24.15.0 <25`、npm 11。

從 repository root 執行：

```bash
make test-contracts
make test-backend
make test-rca-worker
make test-frontend
make check
```

各套件的等效命令：

```bash
UV_CACHE_DIR="$PWD/backend/.uv-cache" uv run --project backend pytest backend/tests
UV_CACHE_DIR="$PWD/backend/.uv-cache" uv run --project backend ruff check backend/src backend/tests
UV_CACHE_DIR="$PWD/backend/.uv-cache" uv run --project backend pyright backend/src

UV_CACHE_DIR="$PWD/rca-worker/.uv-cache" uv run --project rca-worker pytest rca-worker/tests
UV_CACHE_DIR="$PWD/rca-worker/.uv-cache" uv run --project rca-worker ruff check rca-worker/src rca-worker/tests
UV_CACHE_DIR="$PWD/rca-worker/.uv-cache" uv run --project rca-worker pyright rca-worker/src

CI=1 NG_BUILD_MAX_WORKERS=1 npm --prefix frontend test -- --watch=false
CI=1 NG_BUILD_MAX_WORKERS=1 npm --prefix frontend run build
```

修改契約、資料庫文件、部署 manifests 或跨服務行為時，至少執行：

```bash
UV_CACHE_DIR="$PWD/backend/.uv-cache" uv run --project backend pytest contracts/compatibility-tests
git diff --check
```

依變更風險補跑受影響服務測試；完成前優先執行 `make check`。若因本機 PostgreSQL、
Pub/Sub Emulator、Node 版本或 sandbox 限制無法執行，必須明確記錄未執行項目與原因，
不得宣稱完整驗證通過。

## Migration 安全規則

Backend 與 Worker 使用獨立 Alembic version table。全新資料庫的固定順序是：

1. Backend `0002_grafana_normalization_v2`
2. Worker `0002_adk_specialist_analysis`
3. Backend `0003_non_partition_runtime_tables`
4. Worker `0003_validate_ordinary_runtime_tables`

執行前必須備份資料庫並停止 Backend、Worker 及所有寫入；每個 gate 後驗證兩個 version
table。不得用 `upgrade head` 取代明確 revision，不得 `stamp`、重播已發布 migration，
也不得自動執行 destructive downgrade。失敗時保持 writes 停止，依核准的備份、
forward-fix 或明確復原計畫處理。

GKE 發布只使用單一入口：

```bash
deploy/k8s/run-migrations.sh
```

Runner 會依相同順序建立四個獨立 Job、逐一等待完成，並在失敗時輸出 describe/log、
停止建立後續 Job。不得把四個 gate 改成同一個 Kubernetes Job 或平行執行。

## GKE 發布規則

- 發布前確認目標 cluster、非空白 namespace、`sre-agent-secrets`、Cloud SQL、Pub/Sub、
  Workload Identity、Gateway 與網路設定。
- Base 與 migration Job 必須使用 registry 的不可變 image digest；不得部署 repository
  manifests 中的 `:latest` reference。
- Migration 全部完成後，才能 `kubectl apply -k deploy/k8s/base`，再依序 restart 並等待
  Backend、Frontend、Worker rollout status。
- 正式環境 Operator API 尚未完成身分驗證，`APP_ENVIRONMENT=production` 時會以
  HTTP 503 fail closed；不得改成 `local` 繞過控制。
- 應用程式可以回滾 image／manifest，但資料庫只能 forward-fix 或依核准程序復原。

## 設定與機密

- 真實 `.env.*`、Bearer token、資料庫密碼、service account key 與其他 credential
  絕對不可提交、輸出到 log 或放進測試 fixture。
- 使用 repository 已提交的 `.env.*.example` 範本；OS 環境變數優先。
- Backend 不得接收模型、MCP URL、MCP manifest 或 evidence budget 等 Worker 設定。
- Frontend runtime 必須從 `/config.json` 取得 `apiBaseUrl`、`locale`、`timeZone`，不得
  在功能程式碼寫死 API base URL。

## Git 與修改原則

- 開始前檢查 `git status --short --branch`、目前 branch 與最新 commits。
- 使用 `codex/` 前綴建立工作分支；不要直接在 `main` 上提交功能變更。
- 工作目錄可能含使用者未追蹤或尚未提交的內容。只修改本次任務需要的檔案，禁止
  刪除、覆寫或回復無關變更。
- 禁止未經明確授權執行 destructive Git 操作、force push 或刪除 branch／worktree。
- 功能、契約或操作方式改變時，同步更新測試與權威文件；不要修改歷史 plan/spec
  來假裝當時設計已不同。
- Commit 與 PR 應保持單一目的，說明實際執行的測試與未執行項目。
