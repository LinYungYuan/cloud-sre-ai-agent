# SRE Agent Backend

Observability RCA 平台的 Backend 服務。

## Grafana 接收執行期

ASGI 進入點是 `sre_agent.api.main:app`。建立或匯入 app 時不會讀取設定與
credential，也不會連線 PostgreSQL。應用程式 lifespan 會驗證設定、載入已啟用的
Grafana source／classification catalog，並建立 SQLAlchemy resource；無效設定或
database／catalog drift 會使啟動失敗。

此階段除了既有平台設定外，還需要以下環境變數：

```sh
export DATABASE_URL='postgresql+asyncpg://app:password@db:5432/sre_agent'
export GRAFANA_TOKENS='{"50000000-0000-0000-0000-000000000001":{"current-2026-08":"opaque-current-token","previous-2026-07":"opaque-previous-token"}}'
export PUBSUB_PROJECT_ID='project-id'
export RCA_TOPIC_ID='rca-jobs'
export APP_ENVIRONMENT='production'
export MODEL_NAME='model-name'
export METRICS_MCP_URL='https://gateway.example/metrics/mcp'
export TRACE_MCP_URL='https://gateway.example/traces/mcp'
export LOG_MCP_URL='https://gateway.example/logs/mcp'
```

`GRAFANA_TOKENS` 是具有固定階層的 JSON：source UUID → 非機密 token ID → 不透明
Bearer credential。Token ID 必須由 1–128 個 ASCII 字母、數字、`.`、`_`、`:` 或
`-` 組成；credential 必須是非空白且不含空格的 ASCII。輪替時暫時同時設定目前與上一版
token ID。系統只保存符合的 token ID；token value 使用 `SecretStr`，絕對不可寫入
log。這些是不透明 Bearer Token，不是 JWT。

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
應用程式。它包含已安裝的 Worker console script、`alembic.ini` 與 `migrations/`，
因此同一個 image 可供 API、Outbox Worker、Migration 與 Partition Maintenance
workload 使用。

使用獨立於應用程式的命令，維持本月與未來兩個月的 partition runway：

```sh
uv run sre-agent-ensure-partitions
# equivalent:
uv run python -m sre_agent.workers.partition_worker
```

Partition 命令只會在執行時讀取 `DATABASE_URL`；若失敗或發現 catalog drift，會回傳
非零 exit code。此命令不會建立或排程任何基礎設施。
