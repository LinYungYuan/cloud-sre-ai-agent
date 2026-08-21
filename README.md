# SRE Agent Platform

This monorepo contains three independently buildable and deployable packages for
the SRE Agent platform:

- `backend/`: FastAPI webhook and Operator REST API.
- `rca-worker/`: Pub/Sub consumer, ADK/MCP orchestration, evidence, and RCA reports.
- `frontend/`: Angular operator interface in Traditional Chinese.

`contracts/` is not a fourth deployable service. It contains versioned OpenAPI,
JSON Schema, examples, database ownership metadata, and compatibility tests used
by the three packages. Packages may consume these published formats but must not
import one another's source code. Infrastructure provisioning is outside this
repository.

## Prerequisites

- Python 3.11 or later and [uv](https://docs.astral.sh/uv/)
- Node.js `>=24.15.0 <25` and npm 11

## Backend setup

Set up the backend from its project directory. The cache location keeps uv's
downloaded artifacts inside this repository instead of a user-home cache.

```bash
cd backend
UV_CACHE_DIR="$PWD/.uv-cache" uv sync --all-groups
```

Run backend tests and static analysis independently:

```bash
cd backend
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check src tests
UV_CACHE_DIR="$PWD/.uv-cache" uv run pyright src
```

From the repository root, the equivalent command is:

```bash
make test-backend
```

## Frontend setup

Install the Angular 22 application dependencies, then run its test suite or a
production build:

```bash
cd frontend
npm ci
npm test -- --watch=false
CI=1 NG_BUILD_MAX_WORKERS=1 npm run build
```

`make test-frontend` uses the current `PATH` for Node and npm. If Node is in a
non-default location, prepend its bin directory without changing the Makefile:

```bash
NODE_BIN_DIR=/path/to/node/bin make test-frontend
```

The frontend retrieves data only through the REST API. Refresh the page manually
to retrieve the latest data.

Provider 判斷只有一條規則：alert labels 存在且非空的
`resource.label.project_id` 就是 GCP；key 不存在就是 AWS。Grafana `folder` 是
專案／系統代碼，Incident identity v2 使用 `sourceId + folder + alertname`；
`AlertValues` 原樣作為不可信 issue 交給 RCA。即使 normalization 是
`VALIDATION_FAILED` 或 `UNCLASSIFIED`，仍建立 Incident、RCA run、worker job 與
outbox event。Angular 只在使用者按「重新整理」時重新讀 REST；Chat/SSE/WebSocket
保留至日後獨立的 `sre-chat-backend` 設計。

## RCA Worker setup

The RCA Worker is a separate Python project with its own dependencies, lock file,
tests, migrations, Dockerfile, startup command, image, and release. It must not
import `backend/src`; Backend must not import `rca-worker/src`.

```bash
cd rca-worker
UV_CACHE_DIR="$PWD/.uv-cache" uv sync --all-groups
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check src tests
UV_CACHE_DIR="$PWD/.uv-cache" uv run pyright src
```

Worker migrations use `alembic_version_rca_worker` and are applied only after
the Backend migrations have reached their required revision.

## Contracts

Contracts prevent independently released packages from silently disagreeing on
payload fields, API shapes, or database ownership. They contain data formats and
compatibility checks only; they contain no business logic and are not deployed.

Contract compatibility tests validate the OpenAPI documents and checked-in
examples using the backend project's development dependencies:

```bash
UV_CACHE_DIR="$PWD/backend/.uv-cache" uv run --project backend pytest contracts/compatibility-tests
```

Or use:

```bash
make test-contracts
```

## Runtime configuration

The backend ASGI entry point is `sre_agent.api.main:app`. Backend settings are
validated during application lifespan startup rather than module import. See
[`backend/README.md`](backend/README.md) for the required `DATABASE_URL`, opaque
Grafana bearer-token JSON format, rotation guidance, and the independently
runnable partition-maintenance command. Invalid backend configuration fails
startup instead of turning valid webhook requests into generic 500 responses.

Backend, RCA Worker, and both Alembic migration streams use one shared Cloud SQL
PostgreSQL 18 application role. Angular never connects to PostgreSQL. The role
has application DML and migration DDL, but no superuser, role-management,
database-owner, or unrelated-schema privileges. New environments use that role
to run Backend migrations (`alembic_version_backend`) before RCA Worker
migrations (`alembic_version_rca_worker`). The legacy `0001_alert_incident_schema` revision
predates the package split; the implementation plan migrates its version-table
metadata without rerunning its DDL. Future migration ownership is defined by
`contracts/database/table-ownership.yaml` once that planned contract is added;
it is enforced by compatibility tests rather than separate database login roles.

For local Pub/Sub delivery, start the Google official emulator and configure
both processes with `PUBSUB_EMULATOR_HOST=127.0.0.1:58085`; configure the RCA
Worker with `PUBSUB_AUTO_CREATE=true`:

```bash
docker compose up -d pubsub-emulator
cd backend && UV_CACHE_DIR="$PWD/.uv-cache" uv run sre-agent-outbox-worker
```

Production does not set `PUBSUB_EMULATOR_HOST`; Google clients use ADC and
Workload Identity, and the RCA Worker keeps `PUBSUB_AUTO_CREATE=false`. No
service-account key is stored in this repository.

Before Angular starts, the frontend loads `/config.json`. Deployments must serve
all of these fields:

```json
{
  "apiBaseUrl": "/api/v1",
  "locale": "zh-TW",
  "timeZone": "Asia/Taipei"
}
```

Application code should inject `RUNTIME_CONFIG` rather than hard-code an API
base URL. Local configuration files matching `.env*` are ignored; commit a
`.env.example` template when one is needed.

## GKE deployment

The portable Kubernetes base and the ordered Backend-then-Worker migration
release procedure are documented in [`deploy/k8s/README.md`](deploy/k8s/README.md).
Namespace, immutable registry digests, Workload Identity bindings, Secret data,
Gateway routing, and Terraform-managed dependencies are environment inputs and
are intentionally not embedded in the base.

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

Backend 未將 ASGI server 固定在應用程式依賴內；本機可用 `uv --with` 暫時提供
Uvicorn，而不修改 lock file：

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
UV_CACHE_DIR="$PWD/.uv-cache" uv run --with uvicorn \
  uvicorn sre_agent.api.main:app --host 127.0.0.1 --port 8000
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
export GRAFANA_TOKENS='{"50000000-0000-0000-0000-000000000001":{"local":"local-dev-token"}}'
export APP_ENVIRONMENT='local'
export MODEL_NAME='gemini-2.5-flash'
export METRICS_MCP_URL='https://localhost.invalid/metrics/mcp'
export TRACE_MCP_URL='https://localhost.invalid/traces/mcp'
export LOG_MCP_URL='https://localhost.invalid/logs/mcp'
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

## Full verification

After all three packages have been set up, run the full repository gate from the root:

```bash
make check
```

It validates contracts, runs Backend and RCA Worker tests plus their independent
Ruff/Pyright gates, then runs the Angular tests and production build.
