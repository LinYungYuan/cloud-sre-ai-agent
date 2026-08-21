# SRE RCA Worker

此套件是可獨立部署的 Pub/Sub 消費者與 RCA 編排器。它只與 Backend 共用版本化契約及
PostgreSQL application role，絕不匯入 Backend 原始碼。

## 正式環境 Image

從儲存庫根目錄建置可獨立部署的 Worker image：

```bash
docker build -t sre-agent-rca-worker:gke-plan rca-worker
```

image 使用 Python 3.12.9 multi-stage build 與 frozen Worker dependency lock。
runtime 只包含 Worker virtual environment、套件原始碼、Alembic 設定與 Worker
migration；不會複製或匯入 `backend/src`。它以 numeric UID/GID `65532:65532`
執行，預設命令是 `sre-agent-rca-worker`。Migration 仍可由明確的 migration Job
執行，例如：

```bash
docker run --rm --entrypoint alembic sre-agent-rca-worker:gke-plan upgrade head
```

## 本機執行

從 repository root 執行 `docker compose up -d postgres pubsub-emulator`，並設定
`PUBSUB_EMULATOR_HOST=127.0.0.1:58085`、`PUBSUB_PROJECT_ID=sre-agent-test` 與
`PUBSUB_AUTO_CREATE=true`。
依序套用 Backend 與 Worker migrations，再啟動 outbox publisher 及
`sre-agent-rca-worker`。官方 Emulator 只供本機開發／整合測試；production 不設定
emulator host，Pub/Sub 使用 ADC／Workload Identity 並維持
`PUBSUB_AUTO_CREATE=false`（預設）。

RCA 總期限 300 秒、claim lease 60 秒、最多 3 次。只有 terminal DB commit 後才
ack；暫時性失敗 nack。MCP 不傳 authentication material，只暴露通過 endpoint、
schema hash 與 read-only annotation 驗證的 capability。本套件不含 Chat、SSE、
WebSocket 或 `sre-chat-backend`，也不執行任何修復 mutation。

Worker 啟動前必須設定 `DATABASE_URL`、`PUBSUB_PROJECT_ID`、`RCA_TOPIC_ID`、
`PUBSUB_SUBSCRIPTION_ID`、`APP_ENVIRONMENT` 與 `MODEL_NAME`。三個 MCP URL 有核准的
預設值，只有 startup configuration 可覆寫。`MCP_CAPABILITY_MANIFEST` 是 JSON
array，每筆包含 `endpoint_identity`、canonical `capability`、
`tool_name_pattern`、完整 `input_schema`（可附 `input_schema_hash`）與
`risk: "READ_ONLY"`。空 manifest 會 fail closed：仍產生 `PARTIAL` RCA，但不連
MCP。實際 tool 必須通過 `tools/list`、endpoint、name pattern、schema hash 與
read-only annotation 才能呼叫。

`AlertValues` 會以 `untrusted: true` 的獨立 data field 傳給 root RCA ADK Agent。
specialist evidence 必須先保存，root Agent 只收到安全摘要與 opaque evidence
references，不收到 raw evidence bytes；structured output 還要通過 status 與 citation
驗證才能保存。
