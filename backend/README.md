# SRE Agent Backend

Observability RCA 平台的 Backend 服務。

## Grafana 接收執行期

ASGI 進入點是 `sre_agent.api.main:app`。建立或匯入 app 時不會讀取設定與
credential，也不會連線 PostgreSQL。應用程式 lifespan 會驗證設定、載入已啟用的
Grafana source／classification catalog，並建立 SQLAlchemy resource；無效設定或
database／catalog drift 會使啟動失敗。

此階段需要以下環境變數；可透過 `.env.backend-api` 或 OS environment 提供，
OS environment 優先，明確指定但不存在的 env file 路徑必須 fail closed：

```sh
export DATABASE_URL='postgresql+asyncpg://app:password@db:5432/sre_agent'
export GRAFANA_TOKENS='{"50000000-0000-0000-0000-000000000001":{"current-2026-08":"opaque-current-token","previous-2026-07":"opaque-previous-token"}}'
export PUBSUB_PROJECT_ID='project-id'
export RCA_TOPIC_ID='rca-jobs'
export APP_ENVIRONMENT='production'
```

`MODEL_NAME`、`METRICS_MCP_URL`、`TRACE_MCP_URL`、`LOG_MCP_URL`、MCP manifest
與 evidence budget 屬於 RCA Worker 設定，不得注入 Backend 容器。

`GRAFANA_TOKENS` 是具有固定階層的 JSON：source UUID → 非機密 token ID → 不透明
Bearer credential。Token ID 必須由 1–128 個 ASCII 字母、數字、`.`、`_`、`:` 或
`-` 組成；credential 必須是非空白且不含空格的 ASCII。輪替時暫時同時設定目前與上一版
token ID。系統只保存符合的 token ID；token value 使用 `SecretStr`，絕對不可寫入
log。這些是不透明 Bearer Token，不是 JWT。

## Migration and recovery operations

Backend migration is only gate 1 or gate 3 of the authoritative four-gate
maintenance-window procedure in
[`../docs/database/postgresql-schema.md`](../docs/database/postgresql-schema.md).
Stop writers before the first gate, verify both Alembic version tables after
every gate, and do not restart either runtime until Worker gate 4 and its
catalog checks pass. The operation is forward-only: retained legacy parents
remain, and post-write recovery uses an approved backup/delta plan rather than
a destructive schema rollback.

The API never automatically replays historical outbox backlog. A globally
authorized operator uses the protected single-event or bounded batch recovery
paths documented in the repository [README](../README.md#protected-outbox-recovery);
the API accepts no payload or routing override from that request.

使用已安裝的 ASGI server 啟動應用程式，例如：

```sh
uv run uvicorn sre_agent.api.main:app
```

## 正式環境 Container Image

從儲存庫根目錄建置正式環境 image：

```sh
docker build -t sre-agent-backend:gke-plan backend
```

image 以 UID/GID `65532` 執行、監聽 port `8000`，並使用 Uvicorn 啟動 ASGI
應用程式。同一個 image 可供 API 與 Backend migration Job 使用。
