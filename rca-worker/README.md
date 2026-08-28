# SRE RCA Worker

這是獨立部署的 Pub/Sub RCA worker。它只與 Backend 共用版本化契約與 PostgreSQL
application role，不匯入 Backend 原始碼，也不執行任何修復 mutation。

## 架構與安全邊界

ProductionRcaProcessor 是 deterministic coordinator。它先載入 Skills、授權
provider/safe scope，再依 Skill 的 canonical capability 做 discovery、endpoint/tool/
schema/read-only 驗證與 deterministic routing；模型不能選擇 endpoint、MCP tool、scope、
time window、query 或 mutation。`metrics.query`、`trace.query`、`log.query` 缺少
安全且 exact 的 manifest/schema match 時，只跳過對應 specialist。

完成實作後共有四個 Google ADK `LlmAgent`：

| Agent | exact name | Skill | model 可見的 tools |
| --- | --- | --- | --- |
| Metrics Specialist | `metrics_specialist_agent` | `metrics-analysis` | `collect_evidence()`、`read_evidence_chunk(evidence_id, chunk_index)` |
| Trace Specialist | `trace_specialist_agent` | `trace-analysis` | 同上 |
| Log Specialist | `log_specialist_agent` | `log-analysis` | 同上 |
| Root RCA | `rca_agent` | `rca-analysis` | `tools=[]` |

三個 Specialist 只取得同一 RCA run、同一 specialist 的 run-scoped Python tools。工具
closure 持有已批准的 scope、window、allowlisted tools、client、deadline 與 persistence；
MCP endpoint、tool name、schema、arguments 和 retry policy 都留在 deterministic Python
邊界。AWS、缺少 scope 或 unsafe GCP scope 直接產生誠實的 `PARTIAL` report，不做 MCP
discovery、MCP network call，也不建立 Specialist 或 Root model。

### Evidence-first 資料流

`collect_evidence()` 不接受模型控制的 routing 參數。它會再次驗證 scope，呼叫 endpoint-
bound read-only MCP、檢查 response byte limit，將完整 raw/structured evidence 在同一
transaction 保存，commit 成功後才建立 receipt/chunks 並回給 Agent。receipt 只有 opaque
`EvidenceReference`；chunk read 每次都以 `(rca_run_id, specialist_run_id, evidence_id)`
驗證 ownership，不接受跨 run 或跨 specialist 的讀取。

相同 `(rca_run_id, specialist)` 的重複 collection 會重用已 commit 的 evidence，不重新
呼叫 MCP；並行 branch 由資料庫 reservation 序列化。chunk 是從已保存 structured JSON
deterministic rebuild，不保存另一份 raw chunk。超大 response fail closed；可接受但被
chunk budget 截斷的輸入只允許 `PARTIAL`，並加入 `ANALYSIS_INPUT_TRUNCATED`。

`EvidenceReceipt.total_chunks` 是 receipt 內所有 evidence references 的 chunk 總數；
`EvidenceChunk.chunk_index` 則只在各自的 evidence reference 內從零開始計算。為維持跨
process 的 exactly-once collection reservation，PostgreSQL advisory-lock transaction 會
在 collection 與 specialist analysis 完成前持有同一個 connection；production pool size
必須按同時執行的 reserved branches 留出額外容量，否則長分析可能造成 connection 等待。

Specialist output 是 validated `SpecialistAnalysisDraft`：最多 20 observations；每個
非 `MISSING` observation 必須引用同一 specialist 的 known evidence；analysis audit 只
保存 validated JSON、model/Skill/hash/timestamp，不複製 raw telemetry。Root ACTIVE
只收到固定順序的 validated observations 與 opaque references，永遠不收到 raw telemetry
或 legacy generic `Finding`。

Worker migration head 是 `0003_validate_ordinary_runtime_tables`，增加 Specialist analysis audit
欄位、`PARTIAL` status 與 stable failure-code constraints，並驗證 Backend `0003_non_partition_runtime_tables`
轉換的普通表結構與 UUID-only FK。可使用的 stable codes 是：

`NO_SAFE_MCP_CAPABILITY`、`MCP_TIMEOUT`、`MCP_TRANSPORT`、`MCP_PAYLOAD_TOO_LARGE`、
`MCP_RESULT_INVALID`、`ANALYSIS_TIMEOUT`、`ANALYSIS_SCHEMA_INVALID`、
`ANALYSIS_UNKNOWN_EVIDENCE`、`ANALYSIS_INPUT_TRUNCATED`、`ANALYSIS_FAILED`；job
terminal status also uses the stable `DEADLINE_EXCEEDED`、`VALIDATION_FAILED`、
`INTERNAL_ERROR` codes.

## 設定與硬上限

預設 rollout 是 `DISABLED`。所有值在 startup 由 `WorkerSettings` 驗證，production
只能使用表中的上限或更小值；不能藉由環境變數提高 context、network、tool-call 或
deadline budget。`EVIDENCE_MAX_TOTAL_CHARS` 另外必須不超過
`EVIDENCE_CHUNK_CHARS * EVIDENCE_MAX_CHUNKS`。

| 環境變數 | 預設值 | 可接受上限／允許值 | 安全用途 |
| --- | ---: | --- | --- |
| `SPECIALIST_ANALYSIS_MODE` | `DISABLED` | `DISABLED`、`SHADOW`、`ACTIVE` | 明確控制資料流；不以新版部署自動啟用 |
| `MCP_MAX_RESPONSE_BYTES` | `2097152` | `2097152`（2 MiB） | MCP response 超過即 fail closed，不保存部分 evidence |
| `EVIDENCE_CHUNK_CHARS` | `8000` | `8000` | 單一 model tool response 的 Unicode code-point 上限 |
| `EVIDENCE_MAX_CHUNKS` | `4` | `4` | 每筆 evidence 可提供的 chunk 數 |
| `EVIDENCE_MAX_TOTAL_CHARS` | `32000` | `32000` | 每筆 evidence 可提供的 structured 字元總量 |
| `SPECIALIST_MAX_TOOL_CALLS` | `5` | `5` | 單一 Specialist 的 collect/read tool-call 上限 |
| `SPECIALIST_MAX_OBSERVATIONS` | `20` | `20` | 單一 Specialist output 的 observation 上限 |
| `RCA_DEADLINE_SECONDS` | `300` | `300` 秒 | job、MCP、analysis 與 Root synthesis 的全域 deadline |
| `AGENT_CORRECTIVE_RETRIES` | `1` | `1`（可設 `0`） | schema/citation invalid 時最多一次安全 correction |

`MCP_CAPABILITY_MANIFEST` 仍是 startup-only 的 trusted JSON array；每筆必須是 endpoint-
bound、read-only、具 canonical capability、tool-name pattern 與完整 input schema。它不
屬於上述九個分析 budget，但空 manifest 會 fail closed。Worker 啟動前另需設定
`DATABASE_URL`、`PUBSUB_PROJECT_ID`、`RCA_TOPIC_ID`、`PUBSUB_SUBSCRIPTION_ID`、
`APP_ENVIRONMENT` 與 `MODEL_NAME`。MCP URL 只能由 validated startup configuration
覆寫，且不傳 authentication material。

## 本機啟動與驗證

本機只使用 repository 既有的 PostgreSQL 與 Google 官方 Pub/Sub Emulator，不連 GCP
production：

```bash
# Run these commands from the repository root.
docker compose --env-file .env.compose.example up -d postgres pubsub-emulator
export PUBSUB_EMULATOR_HOST=127.0.0.1:58085
export PUBSUB_PROJECT_ID=sre-agent-local
export PUBSUB_AUTO_CREATE=true
export MIGRATION_TEST_DATABASE_URL='postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent'

# 依序執行四個明確 revision 命令，不得跨越未驗證 gate
(cd backend && BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade 0002_grafana_normalization_v2)
(cd rca-worker && RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade 0002_adk_specialist_analysis)
(cd backend && BACKEND_MIGRATION_ENV_FILE=../.env.backend-migration.example uv run alembic upgrade 0003_non_partition_runtime_tables)
(cd rca-worker && RCA_WORKER_MIGRATION_ENV_FILE=../.env.rca-worker-migration.example uv run alembic upgrade 0003_validate_ordinary_runtime_tables)

# Run the full Backend and RCA Worker suites against the migrated database and emulator.
(cd backend && UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest -v)
(cd rca-worker && UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest -v)
```

production 不設定 `PUBSUB_EMULATOR_HOST`，使用 ADC/Workload Identity，並維持
`PUBSUB_AUTO_CREATE=false`。只有 terminal DB commit 後才 ack；暫時性 transport failure
才依 durable retry policy nack。

## Rollout gate

依序執行下列階段；每次改 mode 後重啟 Worker deployment，並保留 migration head。

1. **DISABLED baseline** — `SPECIALIST_ANALYSIS_MODE=DISABLED`。安全 GCP path 仍走
   legacy MCP collection 與 `synthesize_legacy`，建立 0 個 Specialist model，
   `specialist_runs.analysis_result` 維持 NULL；AWS/no-safe path 仍為 no-MCP、no-model
   的 `PARTIAL`。先記錄 report status、MCP calls、latency、failure code 與 evidence count。
2. **SHADOW audit** — 改為 `SHADOW`。同一 evidence 只能 collection 一次並可 reuse；三個
   Specialist ADK agents 產生並保存 analysis audit，但 Root 仍使用 legacy summaries。
   觀測 latency < 300 秒、tool/byte/chunk budget、analysis status、truncation/failure
   rates、observation quality 與 citation validity；任何 cross-owner read、raw leakage、
   endpoint/tool/query mutation 或不穩定 code 都是 gate failure。
3. **ACTIVE gate** — 只有 SHADOW audit 通過才改為 `ACTIVE`。Root 只收 validated
   observations/opaque refs，仍是 `tools=[]`；citation validity 必須 100%，cross-owner
   read、raw telemetry、generic `Finding` 與 model-controlled MCP arguments 必須為 0，
   每個 job latency 必須小於 300 秒。部分 specialist failure 必須保留其他 evidence/
   analyses 並降級 RCA 為 `PARTIAL`；全部 routed specialists 無 usable observations
   時才是 `FAILED`。

### Rollback

Rollback 只改 application mode，不做 destructive migration downgrade；既有 evidence、
analysis audit 與 schema 保留。Kubernetes 可執行：

```bash
kubectl -n "$NAMESPACE" patch configmap sre-agent-config --type merge \
  -p '{"data":{"SPECIALIST_ANALYSIS_MODE":"DISABLED"}}'
kubectl -n "$NAMESPACE" rollout restart deployment/sre-agent-rca-worker
kubectl -n "$NAMESPACE" rollout status deployment/sre-agent-rca-worker
```

確認新 pod 的 `SPECIALIST_ANALYSIS_MODE=DISABLED` 後，報告會回到 legacy
evidence-summary RCA path；不需回退 migration，已保存 evidence/analysis 仍可稽核與讀取。

## 常用檢查

```bash
cd rca-worker
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff format --check .
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check .
UV_CACHE_DIR="$PWD/.uv-cache" uv run pyright
```

正式環境 image 可從 repository root 建置：

```bash
docker build -t sre-agent-rca-worker:gke-plan rca-worker
docker run --rm --entrypoint alembic sre-agent-rca-worker:gke-plan upgrade head
```

runtime 只包含 Worker virtual environment、套件原始碼、Alembic 設定與 Worker
migrations，以 numeric UID/GID `65532:65532` 執行；不會複製或匯入 `backend/src`。
