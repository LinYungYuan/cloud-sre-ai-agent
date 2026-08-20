# Observability RCA Multi-Agent Platform 設計規格

> **部分內容已由新版規格取代：** Grafana provider/identity、未分類 RCA 及本期聊天功能範圍，請以 [`2026-08-13-grafana-normalization-rca-worker-design.md`](./2026-08-13-grafana-normalization-rca-worker-design.md) 為準。聊天室、共享調查對話、人工追問、conversation worker 與 realtime channel 已保留至未來獨立設計，不屬於目前 release scope。

**狀態：** 已核准

**日期：** 2026-08-12

**語言：** 使用者介面與 AI 說明採繁體中文（zh-TW）

## 1. 目標

本系統是一套公司內部使用的 SRE AI Agent 平台。它接收 Grafana Alerting webhook，自動建立 Incident，透過 Metrics、Trace、Log specialist agents 收集證據，再由 RCA Agent 產生可追溯的根因分析。工程師可查看進度、指派處理人、重跑 RCA、結案及重新開啟；共享對話與聊天已移出本階段。

系統的核心原則是：

- Router 決定由哪個 agent 處理問題。
- Skill 定義調查方法、證據契約及安全規則。
- Google ADK 提供 agent runtime 與 orchestration。
- MCP 提供真實 observability 資料。
- Agentgateway 控制 MCP 存取、授權與稽核。
- Specialist agents 只陳述各自領域的 evidence，不自行宣稱 root cause。
- RCA Agent 負責 evidence correlation、competing hypotheses 與結論。
- 所有 observed facts 必須能追溯至原始 evidence。

## 2. 已確認的需求

### 2.1 產品行為

- 本 release 支援 Grafana 告警自動觸發與唯讀 Operator REST/UI；工程師追問保留給未來獨立 Chat service。
- 每個新的 `firing` Incident 都自動啟動 RCA。
- Grafana 重送或相同 alert instance 更新不得重複建立 Incident 或 RCA run。
- Grafana `resolved` 更新 alert lifecycle，但不自動結束 Incident。
- 同一 Incident 使用稽核時間軸；共享調查對話不屬於目前 release scope。
- 使用者可確認、指派、留言、手動重跑 RCA、解決及重新開啟 Incident。
- 所有資料永久保存在 Cloud SQL PostgreSQL 18，不使用外部冷儲存。
- 預估流量低於每日 1,000 個 alert instances。

### 2.2 組織與權限

- 系統供單一公司使用。
- 應用團隊只能查看自己負責的 team/project/environment scope。
- 中央 SRE 可被授予跨團隊或全域權限。
- Identity Provider 先保留，不綁定 IAP、Keycloak、Okta 或其他實作。
- 生產環境未設定 Identity Provider 時必須 deny by default。
- Mock identity 僅可在本機開發環境啟用。

### 2.3 體驗與服務目標

- 合法 webhook 在 2 秒內完成接收並回傳。
- 新 Incident 在 webhook 接收後 5 秒內可由 authenticated Operator REST API 查詢；使用者重新整理後即可看到最新狀態。
- 完整 RCA 目標在 5 分鐘內完成。
- 超過 deadline 時必須保存已有 evidence，並產生 `PARTIAL` 或清楚的失敗狀態。
- Angular 畫面、狀態、提示及錯誤訊息使用繁體中文。
- AI RCA 報告與共享調查回覆使用繁體中文。
- service、metric、span、trace ID、exception、log pattern 及原始 evidence 維持原文。

## 3. 範圍

### 3.1 本規格包含

- Python/FastAPI backend API。
- Google ADK multi-agent RCA runtime。
- Grafana webhook ingestion。
- Cloud SQL PostgreSQL 18 schema 與 migration。
- Durable asynchronous RCA workflow。
- Angular frontend。
- Versioned OpenAPI contracts。
- 前後端測試、agent evaluation、observability 及安全邊界。

### 3.2 本規格不包含

- Terraform。
- Kubernetes manifests。
- GKE、Cloud SQL、Pub/Sub、Secret Manager 或 Load Balancer 的資源建立。
- Identity Provider 的具體選型與串接。
- 自動 remediation，例如 restart、rollback、scale、delete 或 apply。
- Grafana alert rule 的建立與維護。

上述基礎設施由使用者自行處理；應用程式只定義必要的環境變數、連線契約及 readiness requirements。

## 4. 架構決策

採用「三個獨立套件、共同 contracts」方案。

```text
Grafana
   │ HTTPS webhook
   ▼
Backend API ─────── Cloud SQL PostgreSQL 18
   │                     ▲
   ▼                     │
Transactional Outbox     │
   │                     │
   ▼                     │
Pub/Sub ─────── RCA Worker
                       │
                       ▼
              ADK / MCP Agents

Angular SPA
   │ Authenticated REST
   ▼
Backend API
```

Backend API 與 RCA Worker 是同一 repository 內的兩個獨立 Python packages。兩者各自擁有 `pyproject.toml`、lock file、tests、Dockerfile、啟動命令、image 與發布流程，且不得互相 import source。AI 任務不得占用 API process。Cloud SQL 是 transactional source of truth；Pub/Sub 是 durable work delivery mechanism；transactional outbox 防止資料已提交但工作事件遺失。

Angular 是獨立 project、image、version 與發布流程，只依賴已發布的 OpenAPI contract，不得 import backend source、SQLAlchemy models 或 ADK events。第一版不提供 browser realtime channel；使用者以頁面重新整理或明確的重新載入操作取得最新狀態。

## 5. Repository 結構

```text
sre-ai-agent-platform/
├── backend/
│   ├── src/sre_agent/
│   │   ├── api/
│   │   │   ├── middleware/
│   │   │   ├── routers/
│   │   │   └── schemas/
│   │   ├── application/
│   │   │   ├── alerts/
│   │   │   └── incidents/
│   │   ├── domain/
│   │   │   ├── alerts/
│   │   │   ├── incidents/
│   │   │   └── identity/
│   │   ├── integrations/
│   │   │   ├── grafana/
│   │   │   ├── pubsub/
│   │   │   └── identity/
│   │   ├── persistence/
│   │   │   ├── models/
│   │   │   └── repositories/
│   │   ├── policy/
│   │   ├── observability/
│   │   └── config/
│   ├── migrations/
│   ├── tests/
│   │   ├── unit/
│   │   ├── integration/
│   │   └── contract/
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── alembic.ini
│   └── Dockerfile
├── rca-worker/
│   ├── src/sre_rca_worker/
│   │   ├── agents/
│   │   │   ├── specialists/
│   │   │   ├── rca/
│   │   │   └── skills/definitions/
│   │   ├── application/rca/
│   │   ├── domain/
│   │   │   ├── evidence/
│   │   │   └── rca/
│   │   ├── integrations/
│   │   │   ├── mcp/
│   │   │   └── pubsub/
│   │   ├── persistence/
│   │   ├── workers/
│   │   └── config/
│   ├── migrations/
│   ├── tests/
│   │   ├── unit/
│   │   ├── integration/
│   │   ├── contract/
│   │   └── eval/datasets/
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── alembic.ini
│   └── Dockerfile
├── frontend/
│   ├── src/app/
│   │   ├── core/
│   │   │   ├── api-client/
│   │   │   ├── auth/
│   │   │   └── error-handling/
│   │   ├── layout/
│   │   ├── features/
│   │   │   ├── dashboard/
│   │   │   ├── alerts/
│   │   │   ├── incidents/
│   │   │   ├── investigation/
│   │   │   ├── unclassified-alerts/
│   │   │   ├── mappings/
│   │   │   └── settings/
│   │   └── shared/
│   ├── tests/unit/
│   ├── tests/e2e/
│   ├── angular.json
│   ├── package.json
│   └── Dockerfile
├── contracts/
│   ├── openapi/
│   │   ├── grafana-webhook-v1.yaml
│   │   └── operator-api-v1.yaml
│   ├── examples/
│   ├── schemas/
│   │   ├── rca-job-message-v1.json
│   │   └── rca-report-v1.json
│   ├── database/
│   │   └── table-ownership.yaml
│   └── compatibility-tests/
├── docs/
│   ├── architecture/
│   ├── adr/
│   ├── database/
│   ├── api/
│   ├── security/
│   └── runbooks/
├── scripts/
├── docker-compose.yml
├── Makefile
└── README.md
```

Repository 不包含 `infrastructure/` 目錄。

## 6. Grafana 告警接收

### 6.1 Endpoint 與驗證

```http
POST /webhooks/v1/grafana/{source_id}
Authorization: Bearer <token>
Content-Type: application/json
```

成功完成資料庫 transaction 後回傳：

```http
202 Accepted
```

```json
{
  "deliveryId": "019...",
  "acceptedAt": "2026-08-12T10:20:31Z"
}
```

Grafana 從外部網路呼叫公開 HTTPS endpoint。Bearer token 由 Secret Manager 注入並支援多把 token 無中斷輪替；authorization header、token 與 cookie 不得出現在 log 或資料庫。Token 驗證失敗的 payload 不保存。

### 6.2 Ingestion transaction

單一 transaction 執行：

1. 保存不可修改的原始 webhook delivery。
2. 展開 payload 中的 `alerts[]`。
3. 正規化 firing/resolved alert events。
4. 執行 deduplication 與 alert instance upsert。
5. 以 `resource.label.project_id` 判斷 provider，並以 `folder + alertname` 建立 identity v2；team/environment/service 不是必要輸入。
6. 建立或更新 Incident。
7. 新 Incident 建立 RCA job 與 outbox event。

格式錯誤回傳 `400`，token 錯誤回傳 `401`，payload 過大回傳 `413`，必要服務不可用回傳 `5xx` 讓 Grafana 重送。

## 7. 去重、分類與 Incident 聚合

### 7.1 去重

- 所有 webhook deliveries 永久保存。
- 使用 source ID、Grafana fingerprint、alert lifecycle timestamps 與 payload identity 建立 idempotency key。
- 重送可建立新的 delivery audit record，但不得重複產生 normalized state transition、Incident 或 RCA run。
- 同一 Incident 同時最多一個 active RCA run。

### 7.2 Scope 分類

Provider 僅由 Grafana label `resource.label.project_id` 決定：存在且非空是 GCP，key 不存在是 AWS，存在但空白／無效則是 GCP `VALIDATION_FAILED`。`folder` 是專案／系統代碼；Incident identity v2 使用 `sourceId + folder + alertname`。`cloud_provider`、ARN、Series 或 AlertValues 都不能覆寫 provider。

Normalization rules 可補充 resource type/id/name，但不能阻止 Incident/RCA 建立。`UNCLASSIFIED` 與 `VALIDATION_FAILED` 仍永久保存，並建立 `RCA_ANALYSIS` worker job 與 outbox event；team、environment、service 不再是新告警的必要欄位。

### 7.3 Incident lifecycle

Alert、Incident 與 RCA 狀態分開管理：

```text
Alert Instance
FIRING → RESOLVED

Incident
OPEN → INVESTIGATING → RESOLVED
  ▲                         │
  └──── reopen command ─────┘

RCA Run
QUEUED → RUNNING → SUCCEEDED
                                           ├─ PARTIAL
                                           ├─ FAILED
                                           └─ CANCELLED
```

`acknowledged_at`、`acknowledged_by` 與 `assigned_to` 是獨立欄位，不是 Incident status。重新開啟是一個受稽核的 command/event，執行後 status 回到 `OPEN`，不是額外的 `REOPENED` status。Grafana 全部恢復時只更新 `alert_state = RESOLVED`；Incident 仍由負責人檢查後手動結案。已結案的相同 fingerprint 再次 firing 時建立新 Incident，並記錄前後 Incident 關聯。

每個新 firing Incident 都會在同一 transaction 建立 RCA run、`RCA_ANALYSIS` job 與 outbox event。工作進入 `QUEUED` 後有 300 秒總期限；沒有安全 GCP scope、AWS 或缺少允許 capability 時不連 MCP，但仍產生清楚標示「證據不足」的 `PARTIAL` 報告。

## 8. Multi-Agent RCA

### 8.1 Agent 責任

- Metrics Agent：找出異常起點、影響範圍、baseline 與變化幅度；不得宣稱 causation。
- Trace Agent：定位 critical path、slow/error spans、retry、fan-out 與 dependency latency；不得在缺乏證據時推測下游故障原因。
- Log Agent：整理 exception、error pattern、stack trace、first/last seen、frequency 與 event sequence。
- RCA Agent：建立 evidence timeline、competing hypotheses、supporting/contradicting/missing evidence、leading hypothesis、confidence 與 verification plan。

Deterministic Rule Router 只依 provider、安全 scope 與啟動時探索到的 read-only capabilities 選擇 specialists；GCP 可並行 Metrics、Trace、Log，再由唯一的 RCA Agent 綜合。Rule Router 不是 LLM agent，也不把 AlertValues 當指令。人工追問與共享對話保留給未來 Chat service。

### 8.2 時間預算

```text
排程與載入上下文       ≤ 30 秒
三個 Specialist 並行  ≤ 180 秒
RCA 綜合與報告        ≤ 60 秒
重試與安全緩衝         ≤ 30 秒
總計                  ≤ 300 秒
```

單一 specialist 失敗不得使整份調查消失。有部分 evidence 時產生 `PARTIAL` 報告；完全失敗時保存錯誤、attempt 與 correlation ID，允許前端重跑。MCP 呼叫必須有 timeout、有限重試與 circuit breaker。

### 8.3 Evidence contract

所有 specialists 回傳共同結構：

```text
SpecialistResult
├── source_agent
├── finding
├── confidence
├── evidence[]
└── next_evidence_source

Evidence
├── evidence_type
├── source_agent
├── source_endpoint
├── tool_name
├── observed_at
├── normalized provider/safe scope
├── time_window_start/end
├── structured_data
├── raw_result BYTEA
└── content_hash／metadata
```

每一項 RCA claim 必須透過關聯表指出 supporting、contradicting 或 missing evidence。Confidence 不可取代 provenance。

## 9. Cloud SQL PostgreSQL 18

### 9.1 組織與權限

```text
teams
projects
environments
services
subjects
scope_grants
```

`subjects.external_id` 保持通用。Backend policy layer 強制 scope filtering；Angular 不是安全邊界。可在 Identity Provider 確定後增加 PostgreSQL row-level security 作為 defense in depth，但 API authorization 仍不可省略。

### 9.2 Grafana 與 Alert

```text
grafana_sources
webhook_deliveries
ingestion_dedup_keys
alert_events
alert_instances
classification_mappings
```

- `webhook_deliveries` 保存 immutable raw JSONB、body hash、source、received time 與處理結果。
- `alert_events` 保存 firing/resolved 歷史。
- `alert_instances` 保存每個 source/fingerprint 的最新狀態。
- `ingestion_dedup_keys` 提供跨時間分區的唯一性防護。
- `classification_mappings` 保存 matcher、priority、target scope、enabled 與 audit metadata。

### 9.3 Incident

```text
incidents
incident_alerts
incident_assignments
incident_status_history
```

`incidents` 至少包含 UUID、可讀 incident number、title、severity、status、alert state、scope IDs、acknowledgement、assignment、opened/resolved timestamps、reopened relation 及 optimistic locking version。

### 9.4 RCA 與 evidence

```text
rca_runs
specialist_runs
evidence_records
rca_hypotheses
hypothesis_evidence
rca_reports
```

每次重跑建立新 RCA run 與 report version，不覆蓋歷史。Evidence 保存 MCP endpoint、capability、tool、time window、provenance、structured JSON、精確 raw bytes 與 content hash；報告只保存 opaque evidence references，不複製 raw evidence。

### 9.5 稽核與非同步工作（Chat 保留）

```text
incident_messages
incident_timeline_events
audit_events
outbox_events
worker_jobs
worker_attempts
```

`incident_messages` 是 legacy reserved/unused table；本 release 不建立 Chat API、message route、conversation job、SSE 或 WebSocket。Timeline、audit、outbox 與 worker records 仍用於 durable workflow；audit records 不可由一般 API 修改或刪除。

### 9.6 永久保存與分區

- `webhook_deliveries`、`alert_events`、`evidence_records`、`incident_messages`、`incident_timeline_events`、`audit_events` 按月分區。
- Incident 與 alert instance current-state tables 不分區。
- 所有時間使用 `TIMESTAMPTZ` 並以 UTC 儲存。
- 原始 payload、labels、annotations 與彈性 evidence 使用 JSONB。
- 常用 scope、status、fingerprint 與時間欄位使用 B-tree indexes。
- 只對確定需要搜尋的 JSONB path 建立 GIN indexes。
- API 強制 cursor pagination 與時間範圍，禁止無限制全表掃描。
- Backend、RCA Worker 與 Alembic migrations 共用同一個 application role；Angular 不連 PostgreSQL。
- 共用 role 具備兩個服務的 DML 與 migrations 所需 DDL，但不具 superuser、role management、database owner 或其他 schema 權限。
- Backend 與 RCA Worker 仍使用不同 Alembic version tables；新環境以同一 role 依序執行 Backend migration，再執行 RCA Worker migration。
- `contracts/database/table-ownership.yaml` 定義唯一 migration owner；兩套 source code 不共享 persistence models，ownership 由 compatibility tests 強制。

## 10. API Contracts

### 10.1 Grafana webhook contract

由 `contracts/openapi/grafana-webhook-v1.yaml` 定義，只提供 machine-to-machine ingestion，不提供任何讀取 API，也不與 Angular 共用認證 middleware。

### 10.2 Operator REST API

主要 endpoints：

```text
GET  /api/v1/dashboard/summary
GET  /api/v1/incidents
GET  /api/v1/incidents/{id}
GET  /api/v1/incidents/{id}/timeline
POST /api/v1/incidents/{id}/acknowledge
POST /api/v1/incidents/{id}/assign
POST /api/v1/incidents/{id}/resolve
POST /api/v1/incidents/{id}/reopen

GET  /api/v1/alerts
GET  /api/v1/alerts/{id}
GET  /api/v1/unclassified-alerts
POST /api/v1/unclassified-alerts/{id}/classify

GET    /api/v1/classification-mappings
POST   /api/v1/classification-mappings
PATCH  /api/v1/classification-mappings/{id}
DELETE /api/v1/classification-mappings/{id}

GET  /api/v1/incidents/{id}/rca-runs
POST /api/v1/incidents/{id}/rca-runs
GET  /api/v1/rca-runs/{id}
GET  /api/v1/rca-runs/{id}/report
GET  /api/v1/rca-runs/{id}/evidence
GET  /api/v1/rca-runs/{id}/hypotheses

GET  /api/v1/incidents/{id}/messages
POST /api/v1/incidents/{id}/messages
```

原始 payload 與 tool results 使用受額外權限保護的 endpoints。列表採 cursor pagination。Mutation 使用 idempotency key 或 `If-Match` optimistic concurrency。錯誤使用 `application/problem+json`，並提供穩定 error code 與 correlation ID。

### 10.3 Browser refresh contract

Operator UI 僅使用 authenticated REST。系統不公開 SSE、WebSocket 或其他 browser realtime endpoint，也不在背景 polling。使用者透過頁面重新整理或明確的重新載入操作取得最新 Incident、alert、RCA 與 message 狀態。內部 outbox/event records 僅供 durable jobs、audit 與 transaction coordination，不是 frontend contract。

## 11. Angular Frontend

### 11.1 技術與語言

- 使用 Angular standalone components 與 lazy-loaded feature routes。
- 第一版使用 Angular signals 與 feature services，不先引入大型全域 state framework。
- OpenAPI generated client 是唯一 REST client contract。
- API base URL 使用 runtime config，不寫死於 build。
- 使用正式 zh-TW i18n resources，不把中文散落寫死在 components。
- API enums 維持英文穩定值，由前端翻譯成繁體中文。
- 日期以 Asia/Taipei 顯示，但 API 與 database 使用 UTC。

### 11.2 導覽

```text
/dashboard
/incidents
/incidents/:incidentId
/alerts
/unclassified-alerts
/classification-mappings
/settings
```

主導覽顯示「總覽、事件、告警、未分類、分類規則、設定」。沒有權限的入口在 UI 隱藏，但真正存取控制仍由 backend 執行。

### 11.3 Dashboard

- Active、Critical、未確認、未指派 Incident 數量。
- RCA queued/running/partial/failed 數量。
- 未分類告警數量。
- 最近 Incident 與最近 24 小時告警趨勢。
- team/project/environment 快速篩選。

### 11.4 Incident 列表與詳情

列表支援 scope、severity、status、RCA status、assignee、時間範圍、搜尋、server-side sorting 及 cursor pagination。列表不自動更新；使用者重新整理或明確重新載入時保留目前 filter 與 sorting query parameters。

詳情頁固定顯示 severity、incident ID、title、scope、alert state、incident status、RCA status、acknowledgement 與 assignee，並提供確認、指派、重跑 RCA、結案及重新開啟。

詳情分為：

- 總覽：Grafana 摘要、時間、labels、annotations、連結及 RCA 摘要。
- 調查：Incident timeline 與 specialist progress；Chat/共享對話不在本 release。
- RCA 報告：leading hypothesis、confidence、supporting/contradicting/missing evidence、verification 與版本比較。
- 告警與證據：關聯 alert instances、evidence、provenance 及受權限保護的 raw data。
- 稽核紀錄：操作人、時間與變更內容。

### 11.5 Alerts、未分類與 mappings

Alerts 頁顯示 lifecycle、source、rule、fingerprint、scope、labels、關聯 Incident、timestamps 與 Grafana links。

未分類頁允許指定 scope、批次套用相同分類、建立 reusable mapping，並在建立前預覽會匹配的現有告警。Mapping 管理頁顯示 priority、source、match conditions、target scope、enabled、last matched 與 audit metadata。

### 11.6 UI 狀態與安全

- 所有頁面提供 loading、empty、partial、error 與 unauthorized 狀態。
- 表格使用 server-side filtering、sorting 與 pagination。
- 大型 JSON 延遲載入。
- 不使用未清理的 `innerHTML` 顯示 telemetry 或 AI 內容。
- 重要狀態不只依賴顏色，並支援鍵盤操作與基本無障礙需求。
- 桌面優先；Incident 摘要與基本操作支援平板及手機。

## 12. 安全與 Policy

- User identity → API authentication → scope authorization → agent policy → allowed MCP → Agentgateway authorization。
- Metrics、Trace、Log agents 只能存取各自 MCP endpoints/capabilities。
- MCP tool names 不寫死於 Skill；啟動時 discovery 並由 capability resolver 建立 allowlist。
- Telemetry、Grafana payload 與 tool results 永遠視為不可信 data，不可覆蓋 system policy、skill 或 agent instruction。
- RCA 第一版完全唯讀。
- 敏感資料遮罩在 backend 完成；Angular 不自行決定是否可顯示 raw evidence。
- 所有狀態、指派、mapping、RCA 與敏感資料讀取寫入 audit trail。
- Webhook endpoint 設定 TLS、content type、payload size、rate limit、timeout 與 replay/dedup controls。
- 若未來安全要求提高，可在 Bearer token 之外加入 Grafana HMAC，不改變 normalized alert schema。

## 13. 錯誤處理

- API 使用穩定英文 error code；Angular 轉成繁體中文。
- 所有錯誤回傳 correlation ID。
- Agent 或 MCP timeout 顯示已取得的部分結果，不偽裝為完整 RCA。
- Worker crash 由 durable delivery 重送，所有 handler 必須 idempotent。
- 同時操作衝突回傳 `409 Conflict` 或 optimistic concurrency error，Angular 提示重新載入。
- 同一 Incident 已有 active RCA run 時，重跑 API 回傳既有 run ID。
- Scope 不完整時 RCA 顯示「等待告警分類」，不得以未知或推測的 service/environment 查詢 MCP。

## 14. Observability

每個 request 建立可串接的 trace：

```text
Webhook/API
→ Ingestion/Application use case
→ Outbox/PubSub
→ RCA Worker
→ Specialist Agent
→ MCP call
→ RCA synthesis
→ Database result
```

至少記錄：latency、route、model calls、token usage、MCP latency/timeout、specialist status、evidence count、RCA status、queue lag、webhook acceptance latency、Incident visibility latency 與 correlation IDs。Logs 不得包含 token、authorization header、未遮罩敏感 payload 或不必要的完整 tool results。

## 15. 測試策略

### 15.1 Backend

- Unit：fingerprint、mapping、state machine、authorization、hypothesis/evidence rules。
- Database：PostgreSQL 18 migrations、partitions、indexes、constraints。
- Grafana contract：firing、resolved、grouped、truncated、duplicate payload fixtures。
- Concurrency：重送、同時 firing、重複 RCA、worker crash。
- Integration：Webhook → Incident → Outbox → Worker → RCA report。
- MCP contract：tool discovery、schema、timeout、authorization。
- Agent eval：route accuracy、evidence fidelity、unsupported causation、RCA quality。
- API contract：OpenAPI backward compatibility。

### 15.2 Angular

- Unit：繁體中文狀態轉換、filter、sorting 與明確重新載入。
- Component：loading、empty、partial、error、unauthorized。
- Contract：generated client 與 operator OpenAPI 一致。
- E2E：告警顯示、確認、指派、RCA、結案與重新開啟；聊天與共享對話不在本階段。
- Accessibility：鍵盤操作、焦點順序、非顏色狀態提示。

## 16. 驗收標準

1. 合法 Grafana webhook 在 2 秒內回傳 `202 Accepted`。
2. 新 Incident 在 webhook 接收後 5 秒內可由 authenticated Operator REST API 查詢，並在使用者重新整理 Angular 後顯示。
3. RCA 在 5 分鐘內完成，或產生可理解、可重跑的 `PARTIAL/FAILED` 狀態。
4. 相同 Grafana delivery、fingerprint update 或 worker redelivery 不產生重複 Incident/RCA。
5. 所有資料永久保存於 Cloud SQL PostgreSQL 18，時間型大型資料表按月分區。
6. 未授權 scope 無法透過 API 或 raw evidence endpoint 讀取。
7. 每個 observed fact 能追溯至 specialist、MCP tool、endpoint、time window 與原始 evidence。
8. Angular 所有系統文字及 AI 說明為繁體中文；原始技術證據不被翻譯或改寫。
9. `backend/`、`rca-worker/` 與 `frontend/` 是三個不可互相 import source 的獨立套件，且可獨立建置、測試、版本化及發布。
10. Repository 不包含 infrastructure code。
11. 未分類 Incident 會自動建立等待中的 RCA run，但在完成 scope 分類前不查詢任何 MCP；完成分類後自動開始執行。

## 17. 後續工作

使用者完成本書面規格審閱後，再建立分階段 implementation plan。實作計畫應先建立 contracts、domain schemas 與可測試的 ingestion path，再加入 real MCP、RCA orchestration 與 Angular UI；不得直接從完整 production deployment 開始。
