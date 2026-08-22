# SRE Agent 平台

此單一儲存庫（monorepo）包含三個可獨立建置與部署的 SRE Agent 套件：

- `backend/`：FastAPI webhook 與 Operator REST API。
- `rca-worker/`：Pub/Sub 消費者、ADK/MCP 編排、證據與 RCA 報告。
- `frontend/`：使用繁體中文的 Angular 操作介面。

`contracts/` 不是第四個可部署服務。它保存三個套件共用的版本化 OpenAPI、JSON
Schema、範例、資料庫所有權中繼資料與相容性測試。各套件可以使用這些已發布格式，
但不得匯入彼此的原始碼。基礎設施建置不在此儲存庫的範圍內。

## 前置需求

- Python 3.11 以上版本與 [uv](https://docs.astral.sh/uv/)
- Node.js `>=24.15.0 <25` 與 npm 11

## Backend 設定

請在 Backend 專案目錄中完成設定。以下快取位置會將 uv 下載的產物保存在此儲存庫，
而不是使用者家目錄的快取中。

```bash
cd backend
UV_CACHE_DIR="$PWD/.uv-cache" uv sync --all-groups
```

獨立執行 Backend 測試與靜態分析：

```bash
cd backend
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check src tests
UV_CACHE_DIR="$PWD/.uv-cache" uv run pyright src
```

在儲存庫根目錄可使用等效命令：

```bash
make test-backend
```

## Frontend 設定

安裝 Angular 22 應用程式相依套件，再執行測試套件或正式環境建置：

```bash
cd frontend
npm ci
npm test -- --watch=false
CI=1 NG_BUILD_MAX_WORKERS=1 npm run build
```

`make test-frontend` 會從目前的 `PATH` 尋找 Node 與 npm。若 Node 安裝在非預設
位置，請將其 `bin` 目錄放在 `PATH` 最前面，不需要修改 Makefile：

```bash
NODE_BIN_DIR=/path/to/node/bin make test-frontend
```

Frontend 只透過 REST API 取得資料；需要手動重新整理頁面才能取得最新資料。

Provider 判斷只有一條規則：alert labels 存在且非空的
`resource.label.project_id` 就是 GCP；key 不存在就是 AWS。Grafana `folder` 是
專案／系統代碼，Incident identity v2 使用 `sourceId + folder + alertname`；
`AlertValues` 原樣作為不可信 issue 交給 RCA。即使 normalization 是
`VALIDATION_FAILED` 或 `UNCLASSIFIED`，仍建立 Incident、RCA run、worker job 與
outbox event。Angular 只在使用者按「重新整理」時重新讀 REST；Chat/SSE/WebSocket
保留至日後獨立的 `sre-chat-backend` 設計。

## RCA Worker 設定

RCA Worker 是獨立的 Python 專案，擁有自己的相依套件、鎖定檔、測試、資料庫遷移
（migration）、Dockerfile、啟動命令、image 與發布流程。它不得匯入
`backend/src`；Backend 也不得匯入 `rca-worker/src`。

```bash
cd rca-worker
UV_CACHE_DIR="$PWD/.uv-cache" uv sync --all-groups
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check src tests
UV_CACHE_DIR="$PWD/.uv-cache" uv run pyright src
```

Worker migration 使用 `alembic_version_rca_worker`，且只能在 Backend migration
到達必要 revision 後套用。

## 契約（Contracts）

契約可避免獨立發布的套件在 payload 欄位、API 結構或資料庫所有權上產生未被察覺的
差異。契約只包含資料格式與相容性檢查，不含商業邏輯，也不會部署成服務。

契約相容性測試使用 Backend 專案的開發相依套件，驗證 OpenAPI 文件與提交至儲存庫的
範例：

```bash
UV_CACHE_DIR="$PWD/backend/.uv-cache" uv run --project backend pytest contracts/compatibility-tests
```

或使用：

```bash
make test-contracts
```

## 執行期設定

Backend ASGI 進入點是 `sre_agent.api.main:app`。Backend 設定會在應用程式 lifespan
啟動期間驗證，而不是在匯入模組時驗證。必要的 `DATABASE_URL`、不透明 Grafana
Bearer Token JSON 格式、輪替方式與可獨立執行的 partition maintenance 命令，請參閱
[`backend/README.md`](backend/README.md)。無效設定會使 Backend 啟動失敗，而不會讓
原本有效的 webhook request 變成通用 HTTP 500 response。

Backend、RCA Worker 與兩條 Alembic migration stream 共用一個 Cloud SQL
PostgreSQL 18 application role；Angular 不會連線 PostgreSQL。該 role 具有應用程式
DML 與 migration DDL 權限，但沒有 superuser、role 管理、database owner 或無關
schema 的權限。新環境會先用此 role 執行 Backend migration
（`alembic_version_backend`），再執行 RCA Worker migration
（`alembic_version_rca_worker`）。舊版 `0001_alert_incident_schema` revision 早於套件
拆分；實作計畫只遷移其 version table 中繼資料，不會重新執行 DDL。加入規劃中的
`contracts/database/table-ownership.yaml` 後，後續 migration 所有權會由該檔案定義，
並透過相容性測試而不是不同的資料庫登入 role 強制執行。

本機 Pub/Sub 傳遞使用 Google 官方 Emulator。兩個程序都需設定
`PUBSUB_EMULATOR_HOST=127.0.0.1:58085`，RCA Worker 另需設定
`PUBSUB_AUTO_CREATE=true`：

```bash
docker compose up -d pubsub-emulator
cd backend && UV_CACHE_DIR="$PWD/.uv-cache" uv run sre-agent-outbox-worker
```

正式環境不設定 `PUBSUB_EMULATOR_HOST`；Google client 使用 ADC 與 Workload
Identity，RCA Worker 維持 `PUBSUB_AUTO_CREATE=false`。此儲存庫不保存任何
service account key。

Angular 啟動前，Frontend 會載入 `/config.json`。部署環境必須提供以下全部欄位：

```json
{
  "apiBaseUrl": "/api/v1",
  "locale": "zh-TW",
  "timeZone": "Asia/Taipei"
}
```

應用程式應注入 `RUNTIME_CONFIG`，不得寫死 API base URL。符合 `.env*` 的本機設定檔
會被忽略；需要範本時請提交 `.env.example`。

## GKE 部署

可攜式 Kubernetes base 與依序執行 Backend、Worker migration 的發布流程，記錄於
[`deploy/k8s/README.md`](deploy/k8s/README.md)。Namespace、不可變 registry digest、
Workload Identity 綁定、Secret 資料、Gateway 路由及 Terraform 管理的相依資源都是
環境輸入，因此不會寫入 base。

## 完整本機啟動

以下流程會啟動 PostgreSQL、Pub/Sub Emulator、Backend、outbox publisher、
RCA Worker 與 Angular frontend。每個長時間運行的程序使用獨立 terminal。

### 1. 準備相依套件

從 repository root 執行：

```bash
(
  cd backend
  UV_CACHE_DIR="$PWD/.uv-cache" uv sync --all-groups --frozen
)
(
  cd rca-worker
  UV_CACHE_DIR="$PWD/.uv-cache" uv sync --all-groups --frozen
)
(
  cd frontend
  npm ci
)
```

前端必須使用 Node.js `>=24.15.0 <25`。先以 `node --version` 確認版本；如果
Node 安裝在非預設位置，將它的 `bin` 目錄放在該 terminal 的 `PATH` 最前面。

### 2. 啟動 PostgreSQL 與 Pub/Sub Emulator

若 `55432` 與 `58085` 都沒有被占用，可直接使用 Compose：

```bash
docker compose up -d postgres pubsub-emulator
```

本次本機環境的 `55432` 已被其他專案占用，因此 PostgreSQL 改用 `55434`，並保留
既有容器不動。首次建立專用資料庫時執行：

```bash
docker volume create sre-agent20_postgres_data
docker run --name sre-agent20-local-postgres \
  -e POSTGRES_DB=sre_agent \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -p 127.0.0.1:55434:5432 \
  -v sre-agent20_postgres_data:/var/lib/postgresql \
  -d postgres:18
```

之後只需重新啟動既有容器：

```bash
docker start sre-agent20-local-postgres
```

本次 `58085` 已有健康的 Google 官方 Pub/Sub Emulator，因此直接沿用。可用以下
命令確認；若無回應且 port 未被占用，再執行
`docker compose up -d pubsub-emulator`：

```bash
curl -fsS http://127.0.0.1:58085/v1/projects/sre-agent-local/topics
```

後續範例均使用：

```bash
export DATABASE_URL='postgresql+asyncpg://postgres@127.0.0.1:55434/sre_agent'
export PUBSUB_EMULATOR_HOST='127.0.0.1:58085'
export PUBSUB_PROJECT_ID='sre-agent-local'
export PUBSUB_AUTO_CREATE=true
export RCA_TOPIC_ID='rca-jobs'
```

若使用 Compose 的預設 PostgreSQL port，將 `DATABASE_URL` 中的 `55434` 改為
`55432`。

### 3. 依序套用兩組 migration

Backend migration 必須先完成，才能套用 Worker migration：

```bash
(
  cd backend
  UV_CACHE_DIR="$PWD/.uv-cache" uv run alembic upgrade head
)
(
  cd rca-worker
  UV_CACHE_DIR="$PWD/.uv-cache" uv run alembic upgrade head
)
```

### 4. 建立最小本機 Grafana catalog

Backend 啟動時會驗證 `GRAFANA_TOKENS` 中的 source 已在資料庫啟用。使用本次的
專用 PostgreSQL 容器時，執行以下 idempotent seed：

```bash
docker exec sre-agent20-local-postgres psql -U postgres -d sre_agent \
  -v ON_ERROR_STOP=1 \
  -c "INSERT INTO teams (id, name) VALUES ('10000000-0000-0000-0000-000000000001', 'Local Team') ON CONFLICT DO NOTHING" \
  -c "INSERT INTO projects (id, team_id, name) VALUES ('20000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 'local-project') ON CONFLICT DO NOTHING" \
  -c "INSERT INTO environments (id, project_id, name) VALUES ('30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001', 'local') ON CONFLICT DO NOTHING" \
  -c "INSERT INTO grafana_sources (id, project_id, environment_id, name) VALUES ('50000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', 'local-grafana') ON CONFLICT DO NOTHING"
```

若使用 Compose PostgreSQL，將 `docker exec sre-agent20-local-postgres` 改為
`docker compose exec -T postgres`。

### 5. 啟動 Backend

Backend 已將 Uvicorn 鎖定在應用程式依賴內；本機直接以 `uv run uvicorn` 啟動：

```bash
cd backend
export DATABASE_URL='postgresql+asyncpg://postgres@127.0.0.1:55434/sre_agent'
export PUBSUB_EMULATOR_HOST='127.0.0.1:58085'
export PUBSUB_PROJECT_ID='sre-agent-local'
export RCA_TOPIC_ID='rca-jobs'
export GRAFANA_TOKENS='{"50000000-0000-0000-0000-000000000001":{"local":"local-dev-token"}}'
export APP_ENVIRONMENT='local'
export MODEL_NAME='gemini-2.5-flash'
export METRICS_MCP_URL='https://localhost.invalid/metrics/mcp'
export TRACE_MCP_URL='https://localhost.invalid/traces/mcp'
export LOG_MCP_URL='https://localhost.invalid/logs/mcp'
UV_CACHE_DIR="$PWD/.uv-cache" uv run uvicorn \
  sre_agent.api.main:app --host 127.0.0.1 --port 8000
```

### 6. 啟動 RCA Worker

Worker 在本機明確設定 `PUBSUB_AUTO_CREATE=true` 時，會 idempotently 建立
`rca-jobs` topic 與 `rca-jobs-local-sub` subscription：

```bash
cd rca-worker
export DATABASE_URL='postgresql+asyncpg://postgres@127.0.0.1:55434/sre_agent'
export PUBSUB_EMULATOR_HOST='127.0.0.1:58085'
export PUBSUB_PROJECT_ID='sre-agent-local'
export PUBSUB_AUTO_CREATE=true
export RCA_TOPIC_ID='rca-jobs'
export PUBSUB_SUBSCRIPTION_ID='rca-jobs-local-sub'
export APP_ENVIRONMENT='local'
export MODEL_NAME='gemini-2.5-flash'
export MCP_CAPABILITY_MANIFEST='[]'
UV_CACHE_DIR="$PWD/.uv-cache" uv run sre-agent-rca-worker
```

空的 `MCP_CAPABILITY_MANIFEST` 會 fail closed：Worker 可以啟動、消費 job，並在
沒有安全 evidence capability 時保存 `PARTIAL` 報告，但不會呼叫 MCP 或模型。
若要產生有 evidence 的完整 RCA，需設定核准的 manifest、可連線的三個 MCP URL，
以及所選 ADK model 所需的 Google AI 或 Vertex AI credentials。

### 7. 啟動 outbox publisher

等本機 Worker 建立 topic 後，再開另一個 terminal：

```bash
cd backend
export DATABASE_URL='postgresql+asyncpg://postgres@127.0.0.1:55434/sre_agent'
export PUBSUB_EMULATOR_HOST='127.0.0.1:58085'
export PUBSUB_PROJECT_ID='sre-agent-local'
export RCA_TOPIC_ID='rca-jobs'
UV_CACHE_DIR="$PWD/.uv-cache" uv run sre-agent-outbox-worker
```

### 8. 啟動 Angular frontend

`frontend/proxy.conf.json` 會把 `/api` 轉送到本機 Backend：

```bash
cd frontend
CI=1 NG_CLI_ANALYTICS=false npm start -- \
  --host 127.0.0.1 --port 4200 --proxy-config proxy.conf.json
```

開啟 <http://127.0.0.1:4200/>。新資料庫的 Incident 清單一開始會是空的。

### 9. 驗證與停止

```bash
curl -fsS http://127.0.0.1:4200/config.json
curl -fsS 'http://127.0.0.1:4200/api/v1/incidents?limit=50'
curl -fsS http://127.0.0.1:58085/v1/projects/sre-agent-local/topics
curl -fsS http://127.0.0.1:58085/v1/projects/sre-agent-local/subscriptions
```

從 repository root 送入既有的 Grafana 範例，可驗證 Backend → outbox →
Pub/Sub → RCA Worker 的完整路徑：

```bash
curl -fsS -X POST \
  'http://127.0.0.1:8000/webhooks/v1/grafana/50000000-0000-0000-0000-000000000001' \
  -H 'Authorization: Bearer local-dev-token' \
  -H 'Content-Type: application/json' \
  --data-binary @contracts/examples/grafana-firing.json
```

成功時 webhook 回傳 HTTP 202 與 `deliveryId`。等待 Worker 消費後重新整理前端；
使用空 manifest 時，最新 RCA 與 worker job 應分別是 `PARTIAL` 和
`SUCCEEDED`，outbox event 應是 `PUBLISHED`。

Backend、Worker、outbox publisher 與 frontend 可在各自 terminal 按 `Ctrl-C`
停止。此專案專用 PostgreSQL 使用：

```bash
docker stop sre-agent20-local-postgres
```

## 完整驗證

完成三個套件的設定後，請從儲存庫根目錄執行完整 gate：

```bash
make check
```

此命令會驗證契約、執行 Backend 與 RCA Worker 測試及各自的 Ruff／Pyright gate，
最後執行 Angular 測試與正式環境建置。
