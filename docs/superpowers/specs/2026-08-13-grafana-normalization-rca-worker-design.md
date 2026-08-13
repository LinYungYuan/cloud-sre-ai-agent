# Grafana 告警正規化與 RCA Worker 設計規格

**狀態：** 已核准

**日期：** 2026-08-13

**介面語言：** 繁體中文（zh-TW）

## 1. 目的與取代關係

本規格定義 Grafana webhook、GCP／AWS provider 判斷、Incident 分組、RCA 排程、Pub/Sub、本機 Emulator、AI 輸入、MCP 安全邊界及相關資料庫調整。

本規格取代下列既有規格內容：

- `2026-08-12-cross-cloud-grafana-alert-design.md` 中要求 Grafana 提供 `cloud_provider`、`cloud_scope_id`、`resource_type`、`resource_id`、`team`、`environment`、`service` 的規則。
- `2026-08-12-observability-rca-platform-design.md` 中以 `team/project/environment/service` 作為告警分類與啟動 RCA 前置條件的規則。
- `2026-08-12-observability-rca-platform-design.md` 中共享調查對話、工程師追問、conversation worker、incident messages 與 realtime channel 的本期範圍。
- 既有 RCA plan 中「未分類告警不得建立或執行 RCA」的規則。
- 既有 RCA plan 中由 LLM 決定 agent/tool 的 AI Router、follow-up worker、conversation API、conversation eval 與任何人工對話工作。RCA Worker 內部的 deterministic Rule Router 不在排除範圍。

未被本規格明確取代的架構、安全、資料保存及操作介面原則仍然有效。

## 2. 已確認的產品需求

- Grafana 直接呼叫 `POST /webhooks/v1/grafana/{sourceId}`。
- `sourceId` 來自 webhook URL，由平台建立 Grafana source 時產生，不由 Grafana body 提供。
- 接受 Grafana 官方標準 webhook JSON，不要求上游組出平台自訂的跨雲 labels。
- 一份 webhook 可包含多筆 `alerts[]`；每筆必須獨立正規化、去重及保存。
- `folder` 是專案／系統代碼。
- 不要求 Grafana 提供 `team`、`environment` 或 `service`。
- `annotations.AlertValues` 是本次告警的主要 issue，格式不固定。
- 未分類或資料不完整的告警仍建立 Incident、RCA、worker job 與 outbox。
- Provider 或安全資源範圍無法確認時，RCA 不查 MCP，但仍產生 `PARTIAL` 報告。
- Grafana `resolved` 只更新機器告警狀態，不自動關閉人工 Incident。
- Angular 不建立 SSE、WebSocket 或其他長連線；使用者重新整理取得最新資料。
- 聊天室與 AI 對話不在本階段範圍。
- Webhook 在 2 秒內完成接收；完整 RCA 目標在 5 分鐘內完成。

## 3. 系統資料流與邊界

```mermaid
flowchart LR
    G["Grafana webhook"] --> A["Backend API：ingestion／Operator REST"]
    U["Angular zh-TW"] -->|"手動重新整理 Operator REST"| A
    A --> N["Provider 判斷與版本化正規化"]
    N --> D["PostgreSQL 18：delivery、alert、Incident、RCA、outbox"]
    D --> O["Outbox publisher"]
    O --> P["Google Pub/Sub／本機 Emulator"]
    P --> W["RCA Worker"]
    W --> M["允許清單內的 MCP"]
    W --> R["Evidence 與 RCA report"]
    C["contracts：OpenAPI／JSON Schema／ownership"] -. "格式驗證" .-> A
    C -. "格式驗證" .-> W
    C -. "產生 REST client" .-> U
```

元件責任：

- Ingestion 只負責驗證、保存、正規化、Incident/RCA 排程及 outbox，不執行 AI。
- Outbox publisher 只發布已提交的工作識別資訊。
- Pub/Sub 只負責 at-least-once 工作傳遞，不作為狀態真相來源。
- RCA Worker 以 PostgreSQL 狀態實現 idempotency、lease、attempt 與 durable settlement。
- Angular 只透過 Operator REST API 讀取資料，不 import backend models。

### 3.1 Monorepo 套件邊界

Repository 固定包含三個可獨立發布的套件：

```text
frontend/      Angular SPA
backend/       FastAPI、Grafana ingestion、Operator REST、transactional outbox
rca-worker/    Pub/Sub consumer、ADK、MCP、RCA orchestration、evidence/report
contracts/     三套件共同格式；不是第四個服務
```

- 三個套件各自擁有 dependency manifest、lock file、tests、Dockerfile、啟動命令、image、CI/build 與版本。
- `rca-worker/` 不得 import `backend/src`；`backend/` 也不得 import `rca-worker/src`。
- 可跨套件共享的只有 `contracts/` 內的 OpenAPI、JSON Schema、examples、database ownership manifest 與 compatibility tests。
- Backend 與 RCA Worker 透過 Pub/Sub message contract、Cloud SQL table contract，以及 Operator 查詢所需的唯讀資料契約協作，不共享 Python domain/application/persistence objects。
- `contracts/` 不含可執行 business logic，不建置成 container，也不部署。

### 3.2 Cloud SQL ownership 與 role

Backend、RCA Worker 與兩套 Alembic migrations 共用同一個 Cloud SQL PostgreSQL 18 application role。Angular 不連線至 PostgreSQL，也不取得這組 credential。

- 共用 application role 具備 Backend 與 RCA Worker 正常執行所需的 DML，並具備兩套 Alembic migrations 所需的 DDL。
- 共用 role 不具 role management、database owner、superuser 或非本系統 schema 的權限。
- 因為資料庫無法用不同 login role 強制隔離兩個 Python 套件，禁止修改 Alert／Incident core schema 等邊界改由 migration ownership contract、source boundary tests 與 code review 強制執行。
- Backend production code 只操作 core tables 與原子排程所需的 `rca_runs`／`worker_jobs`；RCA Worker production code 只讀 core context，並寫入 RCA/worker/evidence/report 及明確允許的 timeline/outbox audit records。

`contracts/database/table-ownership.yaml` 是跨套件 migration ownership contract，不是多角色 grant manifest。Compatibility tests 必須保證每張 table 只有一個 migration owner，且 Backend migration 不修改 Worker-owned tables、Worker migration 不修改 core tables。

Backend 與 RCA Worker 各自使用 Alembic，但使用不同 version tables：

- `backend/migrations/` → `alembic_version_backend`
- `rca-worker/migrations/` → `alembic_version_rca_worker`

兩套 Alembic commands 使用相同 application-role credential。新環境必須先套用 Backend migrations，再套用 RCA Worker migrations。既有 `0001_alert_incident_schema` 是拆包前的 legacy baseline，已建立部分 RCA tables；後續 RCA table DDL 一律由 `rca-worker/migrations/` 演進，Backend 不再新增或修改 worker-owned columns/constraints。Worker migration 必須在執行前驗證所需 Backend schema revision，不得自行建立或修改 core tables。

## 4. Grafana webhook 契約

### 4.1 Endpoint

```http
POST /webhooks/v1/grafana/{sourceId}
Authorization: Bearer <opaque-token>
Content-Type: application/json
```

Bearer 必須在讀取 body 前驗證。驗證失敗時，不得保存或記錄 body。

### 4.2 Envelope

系統接受 Grafana webhook v1，保留以下 envelope 欄位與所有未知 extension fields：

- `receiver`、`status`、`orgId`
- 非空的 `alerts[]`
- `groupLabels`、`commonLabels`、`commonAnnotations`
- `externalURL`、`version`、`groupKey`、`truncatedAlerts`
- `title`、`state`、`message`

每筆 `alerts[]` 保留：

- `status`、`labels`、`annotations`
- `startsAt`、`endsAt`
- `generatorURL`、`fingerprint`
- `silenceURL`、`dashboardURL`、`panelURL`
- `values` 與所有未知 extension fields

系統使用每筆 alert 的 `status`，不得只依 envelope `status` 推定全部 alerts。

### 4.3 核准範例

下列 body 必須成為 checked-in contract fixture，欄位大小寫與原始內容不得在接收時改寫：

```json
{
  "receiver": "My Webhook",
  "status": "firing",
  "orgId": 1,
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "High CPU usage",
        "folder": "COM-LX-BOA-01",
        "severity": "ERROR",
        "DBInstanceIdentifier": "production-rds-01",
        "Series": "123456789012"
      },
      "annotations": {
        "AlertValues": "Account: 123456789012\nDB Name: production-rds-01\nValue: 85.23%\n<br>"
      },
      "startsAt": "2026-08-13T14:30:00+08:00",
      "endsAt": "0001-01-01T00:00:00Z",
      "generatorURL": "https://grafana.example.com/alerting/grafana/abc123/view",
      "fingerprint": "c6eadffa33fcdf37",
      "silenceURL": "https://grafana.example.com/alerting/silence/new?...",
      "dashboardURL": "",
      "panelURL": "",
      "values": {"B": 85.23}
    }
  ],
  "groupLabels": {"alertname": "High CPU usage"},
  "commonLabels": {"folder": "COM-LX-BOA-01", "severity": "ERROR"},
  "commonAnnotations": {},
  "externalURL": "https://grafana.example.com/",
  "version": "1",
  "groupKey": "{}:{alertname=\"High CPU usage\"}",
  "truncatedAlerts": 0,
  "title": "[FIRING:1] High CPU usage",
  "state": "alerting",
  "message": "**Firing**\n\nLabels:\n - alertname = High CPU usage\n - severity = ERROR\n..."
}
```

## 5. Provider 判斷

Provider 只能由每筆 `alerts[].labels` 是否包含精確 key `resource.label.project_id` 判斷：

| 條件 | Provider | 驗證狀態 |
|---|---|---|
| key 存在，且 trim 後有值 | `GCP` | 可繼續驗證與正規化 |
| key 不存在 | `AWS` | 可繼續驗證與正規化 |
| key 存在，但為 `null`、非字串、空字串或只有空白 | `GCP` | `VALIDATION_FAILED` |

硬性限制：

- 不讀取或接受 `cloud_provider` label 作為 provider 來源。
- 不以 `DBInstanceIdentifier`、`Series`、ARN、resource type、`folder` 或文字內容猜 provider。
- DB normalization rule 不得覆寫 provider。
- Provider 沒有 `UNKNOWN` 狀態：key 不存在時一律為 AWS；`UNCLASSIFIED` 只代表資源正規化結果，不代表 provider 未知。
- AWS alert 即使 `Series` 不是 12 位帳號也合法；`Series` 只保存為普通 label。
- GCP project ID 空白時仍保存並建立受限 RCA，但不得使用該值呼叫 MCP。

## 6. 兩層告警模型與正規化

### 6.1 原始層

每份已授權 webhook 永久保存：

- exact raw body bytes（`BYTEA`）
- 解析後 JSONB
- SHA-256
- `sourceId`、token identifier、received timestamp、處理狀態

Raw bytes 不得由 JSON 重新序列化產生。Labels、annotations、message 及 URL 全部視為不可信資料。

### 6.2 Canonical Alert Event

每筆 `alerts[]` 產生一筆 canonical event：

```text
schema_version
source_id
delivery_id
alert_index
status
fingerprint
alert_name
folder_code
severity_raw
severity_canonical
provider
cloud_scope
resource
issue
metric_values
normalization
validation
observed_at
```

其中：

- `alert_name` 來自 `labels.alertname`。
- `folder_code` 來自 `labels.folder`，代表專案／系統代碼。
- `cloud_scope`、`resource` 由安全 normalization rules 擷取；取不到可為空。
- `issue.raw_text` 優先來自 `annotations.AlertValues`。
- `metric_values` 原樣保留 `values`；`display_value` 與 `unit` 只能由規則明確提供，不從 `alertname` 或 AlertValues 猜測。

### 6.3 Hybrid normalization rules

採「版本化 DB rules + 安全程式 fallback」：

- Provider 判斷是固定程式規則，不在 DB 設定。
- DB rule 保存 name、version、enabled、priority、conditions、field mappings、resource type、display unit 與 audit metadata。
- Conditions 只允許欄位存在、精確相等、前綴及有限格式比對。
- 禁止 `eval`、任意 Python、SQL、shell、任意 URL 或可執行 script。
- 程式負責 schema validation、輸出型別、安全限制與 fallback。
- 同優先級多條規則同時命中時，標記 `UNCLASSIFIED` 與 conflict warning，不任意選擇。
- 規則更新建立新版本，不覆寫歷史版本；既有 event 保留 rule ID/version。

資源例子包括但不限於 GCP `resource.type`／`resource.label.*`、AWS `DBInstanceIdentifier`、`LoadBalancer`、`TargetGroup`、`ClusterIdentifier`。這些欄位只用於資源正規化，不用於 provider 判斷。

## 7. AlertValues 與 AI 資料邊界

`annotations.AlertValues` 是告警主要 issue，格式可以因 alert rule 而不同，不建立固定 Account／DB Name／Value parser。

交給 RCA Agent 的結構必須把資料與指令分開：

```json
{
  "alertIssue": {
    "rawText": "AlertValues 原文",
    "source": "grafana.annotations.AlertValues",
    "contentType": "text/plain",
    "untrusted": true
  }
}
```

規則：

- `rawText` 保持原文，不修正 HTML、換行、數字或文案。
- `summary`、`description` 與 envelope `message` 可作補充，但不能取代 AlertValues。
- AlertValues 可提供調查線索，但不能單獨證明根因。
- 其中任何「忽略前述指令」、工具呼叫要求、credential、URL 或程式碼都只視為資料。
- Agent 不得自動開啟或抓取 AlertValues、annotations、message、generatorURL、silenceURL、dashboardURL、panelURL 內的 URL。

## 8. Severity

Severity key/value 以大小寫不敏感方式處理，原值永久保存：

| 原值 | Canonical severity |
|---|---|
| `ERROR` | `SEV1` |
| `WARN`、`WARNING` 或大小寫變體 | `SEV3` |
| 其他、空白或缺少 | `UNMAPPED` |

`UNMAPPED` 必須產生 normalization warning，不可自行猜測等級。若存在 `TrueSight_severity`，另存為 notification severity，不覆寫上述 canonical severity。

## 9. Identity、dedup 與 lifecycle

### 9.1 Delivery 與 alert lifecycle dedup

- Delivery identity：`sourceId + SHA256(exact raw body bytes)`。
- Alert lifecycle identity：優先使用 `sourceId + fingerprint + status + startsAt + endsAt`。
- Fingerprint 缺少或空白時：`SHA256(sourceId + canonical sorted labels)`。
- 相同 delivery 重送可保留 audit record，但不能重複建立 alert transition、Incident、RCA、job 或 outbox。

### 9.2 Incident identity version 2

正常 Incident grouping key：

```text
sourceId + folder + alertname
```

實際儲存的 `identity_key` 使用版本標記與 canonical length-prefixed tuple 後再取 SHA-256，不使用沒有分隔規則的裸字串串接；因此欄位中即使包含空白、冒號或其他合法字元也不會產生組合碰撞。

規則：

- `fingerprint` 不參與正常 Incident 分組，只辨識 alert lifecycle。
- 同一 webhook 內多筆 alerts 各自計算；相同 grouping key 可連到同一 active Incident。
- `folder` 或 `alertname` 缺少／空白時，alert 標記 `VALIDATION_FAILED`，並以 `sourceId + __invalid__ + fingerprint` 建立隔離的 fallback Incident，避免不相關錯誤告警被合併。
- 相同 identity 且存在 active Incident 時沿用。
- 相同 identity 的 Incident 已人工結案，新的 `firing` 建立新 Incident 並保存 reopen relation。
- `resolved` 更新 alert machine state，不修改 human Incident status。
- `groupKey` 與 `groupLabels` 原樣保存，但不作 Incident identity。

## 10. 驗證失敗與未分類 RCA

Provider、folder、alertname、severity 或 resource 正規化不完整時：

1. 保存完整 delivery 與該筆 alert event。
2. 記錄 per-alert `VALIDATION_FAILED` 或 `UNCLASSIFIED`、錯誤欄位與 warnings。
3. 建立或沿用 Incident。
4. 建立 RCA run、`RCA_ANALYSIS` worker job 及 `RCA_RUN_REQUESTED` outbox。
5. 只在能建立安全 MCP scope 時開放對應工具。
6. 無安全 scope 時，僅分析 Grafana issue 與資料庫內既有證據。
7. 證據不足時產生 `PARTIAL` 報告，明確標示「證據不足」，不得猜 provider、資源或根因。

Mixed webhook 中每筆 alert 獨立處理；單筆失敗不得阻止其他 alerts 建立自己的結果。Delivery 若含任何 validation failure，記為 `VALIDATION_FAILED`，HTTP 仍回 `202`。

## 11. Transactional ingestion

單一 PostgreSQL transaction 執行：

1. 保存 immutable delivery（exact bytes、JSONB、hash）。
2. 展開並保存所有 alert events。
3. 對每筆 alert 執行 provider 判斷、normalization、validation 與 dedup。
4. upsert alert instance。
5. 以 identity v2 建立或取得 active Incident，並連結 alert。
6. 必要時建立 active RCA run。
7. 建立唯一 worker job 與唯一 outbox idempotency key。
8. 全部成功後 commit，才回 `202`。

任何中途資料庫錯誤必須整筆 rollback，不得出現有 Incident 卻沒有對應 job/outbox 的狀態。

## 12. Pub/Sub 與官方 Emulator

### 12.1 本機與整合測試

- 本機與 CI 使用 Google 官方 **Pub/Sub Emulator**，不建立自製 broker。
- Local Compose 只提供應用程式開發支援，不包含正式環境 IaC。
- Emulator 由官方 Google Cloud CLI emulator image 啟動，image 必須 pin 至明確版本或 digest。
- 綁定本機／Compose network 所需介面，不對公網開放。
- 應用程式透過 `PUBSUB_EMULATOR_HOST` 與非正式的 local project ID 連線，不使用 GCP credential。
- Topic 與 subscription 由使用相同 Google Cloud Pub/Sub client library 的 idempotent bootstrap 建立；不依賴 Emulator 不支援的 Console 或 `gcloud pubsub` 管理命令。
- 整合測試必須真的完成 publish、pull、redelivery、ack 與 idempotency；單元測試才可使用 fake/mock。
- 每個測試 session 使用隔離 project/topic/subscription 名稱，避免狀態互相污染。

Google 官方說明 Client Libraries 在設定 `PUBSUB_EMULATOR_HOST` 後會連到本機 Emulator；Emulator 與正式服務可能有差異，因此正式環境另保留最小 smoke test：

- <https://docs.cloud.google.com/pubsub/docs/emulator?hl=en>
- <https://cloud.google.com/sdk/gcloud/reference/beta/emulators/pubsub/start>

### 12.2 正式環境

- 使用真正 Google Cloud Pub/Sub。
- 使用 Workload Identity／ADC，不保存 service-account key。
- Project、topic、subscription 由環境設定提供；程式不得在 production startup 任意建立或修改雲端資源。
- Publisher 與 subscriber 使用與 Emulator 相同的 typed adapter；環境差異只存在 composition/configuration layer。

### 12.3 訊息契約

Pub/Sub 不放完整 AlertValues、raw payload、labels、MCP evidence 或 secrets，只放工作識別資訊：

```json
{
  "schemaVersion": 1,
  "workerJobId": "uuid",
  "rcaRunId": "uuid",
  "incidentId": "uuid",
  "attempt": 1
}
```

`attempt` 是建立訊息時的初始值；實際嘗試次數以 PostgreSQL `worker_attempts` 為唯一真相。訊息使用 stable idempotency key，Pub/Sub redelivery 不得建立新 job。

## 13. RCA Worker lifecycle

- Worker 以 `workerJobId + rcaRunId + incidentId + schemaVersion` 驗證訊息與 DB 關係。
- 使用資料庫 row lock／lease 原子 claim 工作。
- RCA deadline 固定為進入 `QUEUED` 後 300 秒。
- Lease 為 60 秒，長任務必須在仍擁有 lease 時安全續租。
- 最多 3 次 durable attempts；實際 attempt 寫入 PostgreSQL，不信任 Pub/Sub message 的值。
- 同一 `workerJobId` 可安全重送；已有 terminal report 時直接 ack。
- Evidence 與 report 成功提交後才 ack。
- 暫時性 transport/MCP 錯誤在剩餘 deadline 與 attempt limit 內 nack／重試。
- Policy、schema、authorization 等永久錯誤持久化 terminal failure 後 ack。
- Worker crash 或 stale lease 可由下一個 delivery 接手，不重複已持久化 evidence。
- 逾時但有可用內容時產生 `PARTIAL`；完全沒有可用內容時為 `FAILED`，不得虛構 leading hypothesis。

## 14. RCA Agent、MCP 與 evidence

### 14.1 Agent input

Worker 從 PostgreSQL 載入 Incident、canonical alert、原始 AlertValues、normalization 結果及允許的工具。Pub/Sub payload 不作分析資料來源。

Google ADK dependency 必須由 `uv.lock` 固定版本，並包在平台 adapter 後方，避免 domain code 依賴易變 SDK API。

### 14.2 詳細 Agent、Route、Skill 與 MCP 架構

```mermaid
flowchart TD
    P["Google Pub/Sub"] --> S["Subscriber"]
    S --> J["RCA Job Handler"]
    J --> L["PostgreSQL job lease<br/>60 秒／最多 3 次／總 deadline 300 秒"]
    L --> C["Incident Context Loader<br/>AlertValues／provider／safe scope／time window"]
    C --> A["RCA Agent<br/>Orchestrator，不直接呼叫 MCP"]
    A --> R["Rule Router<br/>deterministic code，非 LLM Agent"]
    R --> K["Skill Registry"]

    K -->|"GCP＋safe scope＋metrics capability"| MA["Metrics Sub-agent"]
    K -->|"GCP＋safe scope＋trace capability"| TA["Trace Sub-agent"]
    K -->|"GCP＋safe scope＋log capability"| LA["Log Sub-agent"]
    K -->|"AWS 或無 safe scope"| NM["No-MCP analysis"]

    MA --> MS["metrics-analysis Skill"]
    TA --> TS["trace-analysis Skill"]
    LA --> LS["log-analysis Skill"]

    MS --> MM["GCP Metrics MCP<br/>/agw/gcp-metrics-mcp"]
    TS --> TM["GCP Trace MCP<br/>/agw/gcp-trace-mcp"]
    LS --> LM["GCP Log MCP<br/>/agw/gcp-log-mcp"]

    MM --> E["Evidence Validator／Store<br/>BYTEA＋JSONB＋SHA-256＋provenance"]
    TM --> E
    LM --> E
    E --> RA["RCA Agent＋rca-analysis Skill"]
    NM --> RA
    RA --> V["Report Validator<br/>每個 claim 必須引用 evidence 或明示 missing evidence"]
    V --> RP["繁體中文 RCA Report<br/>根因／信心／修復建議／驗證步驟"]
    RP --> D["Terminal DB commit"]
    D --> ACK["Pub/Sub ack"]
```

角色定義：

- **RCA Agent**：唯一的 orchestration 與 synthesis agent。載入 context、呼叫 Rule Router、並行等待選定的 specialists、比較 hypotheses 與反證，最後產生根因、信心程度、修復建議及驗證步驟。RCA Agent 不直接呼叫 MCP。
- **Rule Router**：一般 deterministic code，不是 LLM agent。只根據 provider、safe scope、Skill required capabilities 與啟動時探索到的 MCP capabilities 決定要啟動哪些 specialists；不得把 AlertValues 當作 routing 指令。
- **Metrics Sub-agent**：使用 `metrics-analysis` Skill，分析指標異常、趨勢、延遲與錯誤率，只能看 Metrics MCP tools。
- **Trace Sub-agent**：使用 `trace-analysis` Skill，分析慢節點、錯誤 span 與呼叫鏈，只能看 Trace MCP tools。
- **Log Sub-agent**：使用 `log-analysis` Skill，分析 exceptions、stack traces、錯誤模式與事件時間線，只能看 Log MCP tools。
- **RCA synthesis**：RCA Agent 使用 `rca-analysis` Skill 整理三個 specialists 的已保存 evidence。修復建議只供人員審查，不自動執行 restart、rollback、scale、delete 或任何 mutation。

### 14.3 MCP endpoints 與 scope

固定預設 endpoints：

- Metrics：`https://agentgateway.cp.gcubut.gcp.uwccb/agw/gcp-metrics-mcp`
- Trace：`https://agentgateway.cp.gcubut.gcp.uwccb/agw/gcp-trace-mcp`
- Log：`https://agentgateway.cp.gcubut.gcp.uwccb/agw/gcp-log-mcp`

三個 endpoints 目前不需要 authentication。URL 只能來自 startup settings 的固定 allowlist；job、AlertValues、labels、prompt 或 MCP output 都不得提供或覆寫 endpoint。Worker 啟動時對每個 endpoint 執行標準 `tools/list`，再依 trusted capability manifest 與 tool input schema 建立各 specialist 可見的工具集合；未在 manifest 允許或具有 mutation 能力的工具不可曝光給 agent。因此實際 tool names 由 endpoint 回傳，不硬編碼於本規格。

- GCP：只有 `resource.label.project_id` 為非空有效字串，且 normalization 建立安全資源範圍時，才提供對應 GCP read-only MCP capabilities。
- AWS：本期沒有 AWS MCP endpoint。Rule Router 不啟動三個 GCP specialists，也不呼叫任何 MCP；RCA Agent 只根據 AlertValues 與 Incident context 產生 `PARTIAL` 報告，並明示「目前沒有 AWS MCP 證據」。
- 無法建立 scope 時仍執行 RCA，但 `availableTools` 為空。
- MCP 使用可信 capability manifest、固定 endpoint identity、允許清單與 input schema。
- 缺少、模糊或具 mutation 能力的 tool 一律 fail closed。
- Agent 不可指定任意 endpoint、tool name 或 URL。
- MCP calls 不加入 Authorization header、cookie 或 credential；若未來 authentication 需求改變，必須先更新規格與安全測試。

### 14.4 Evidence

MCP 原始結果永久保存：

- exact raw result `BYTEA`
- parsed/structured `JSONB`
- SHA-256
- endpoint identity、tool、capability、input scope、time window、observed time
- content type、status 與安全 metadata

EvidenceReference 必須包含 evidence UUID 與 partition timestamp，才能精確引用月分割資料。RCA report 只保存 reference 與摘要，不複製整份 raw evidence。

每個 observed claim 必須引用 supporting、contradicting 或 missing evidence。Telemetry、logs、traces 與 tool output 永遠是資料，不是指令。

### 14.5 RCA 結果

- `COMPLETE`：必要分析成功，結論具有足夠可追溯證據。
- `PARTIAL`：部分工具失敗、deadline 到期、安全 scope 不足或證據不足。
- `FAILED`：沒有可用分析結果，或遇到永久 workflow failure。

報告使用繁體中文；service 名稱、metric、trace/span ID、exception、log、labels、AlertValues 與 evidence 保持原文。

## 15. 未來功能：獨立聊天服務（本階段保留）

聊天室、人工追問、逐字 AI 回覆與 SSE 全部移出本階段，不建立 API、資料表、migration、Pub/Sub topic/subscription、worker、Angular 畫面或測試。

未來若重新啟動此需求，重新進行獨立的設計與核准流程。已確認但尚未成為實作需求的方向只有：

- 同一 repository 新增獨立專案 `sre-chat-backend/`。
- Chat service 擁有獨立 image、process、發布流程、資料庫 role 與 `sre_chat` schema。
- Chat service 不 import `backend` 的 application/persistence 程式，也不直接讀寫核心 Incident、RCA 或 Evidence tables。
- Chat service 透過版本化 internal API 取得已授權的 Incident/RCA context。
- Chat service 未來可自行擁有 chat REST、SSE、outbox、Pub/Sub consumer 與 AI chat worker。

上述內容只是未來討論起點，不得被目前 implementation plan、acceptance criteria 或 release scope 引用。現有 schema 若已包含 `incident_messages` 等預留資料表，本階段不得據此推導或實作聊天功能。

## 16. 資料庫遷移與相容性

新 migration 必須向前相容，不修改已套用的 initial migration：

- canonical alert 增加 provider、folder code、alert name、raw/canonical severity、issue、resource、normalization rule/version、validation status 與 warnings。
- Incident 增加 `identity_version` 與 identity v2 必要欄位／索引。
- 新資料使用 identity v2；歷史資料保留原 identity，不離線重算或合併。
- `team_id/project_id/environment_id/service_id` 不再是新 alert 建立 Incident/RCA 的必要條件；migration 與 repository protocol 必須支援 nullable legacy scope。
- `folder_code` 是告警專案／系統代碼，不直接等同既有 `projects.id`。
- Folder 可透過獨立、可稽核的 source-specific mapping 連到內部 authorization scope；沒有 mapping 時仍可建立 RCA，但只允許中央 SRE／具全域權限者查看。
- Source/folder authorization mapping 不得影響 provider 或 Incident identity。
- normalization rules 保存版本與 audit metadata，歷史 event 不因 rule 更新而改寫。
- 新增 evidence raw bytes、metadata/hash 與 evidence reference 所需欄位／約束。
- 所有新 constraint、index、partition 與 downgrade 的資料損失風險同步寫入 PostgreSQL schema reference。

RCA Worker 的 lease、attempt、specialist、evidence、hypothesis 與 report schema 變更不放入 Backend migration；它們由 `rca-worker/migrations/` 與 `alembic_version_rca_worker` 管理。Schema reference 以 table owner 分段，並列出 Backend → RCA Worker 的套用順序與共用 application role。

## 17. HTTP 與操作錯誤

| HTTP | 條件 |
|---|---|
| `202` | Transaction commit；包含 UNCLASSIFIED、GCP project ID 空白、severity unmapped、`truncatedAlerts > 0` |
| `400` | JSON 無法解析、必要 Grafana envelope 錯誤、`alerts` 為空 |
| `401` | Bearer 缺少／錯誤；body 不得被讀取或保存 |
| `413` | 原始 body 大於 1 MiB；達到 1 MiB 整仍合法 |
| `503` | PostgreSQL 暫時不可用，允許 Grafana 重試 |
| `500` | 未預期錯誤；只回安全 correlation ID，不洩漏 credential、SQL、prompt 或內部錯誤 |

`truncatedAlerts > 0` 不拒絕已收到的 alerts，但 delivery 標示 incomplete、產生 metric/warning，避免誤以為通知完整。

## 18. Angular 顯示

介面全部使用繁體中文，並在使用者重新整理後顯示：

- Incident status、alert machine state、RCA status 與更新時間。
- Provider（GCP/AWS）、folder 專案／系統代碼、alertname。
- 原始 severity 與 canonical severity；未映射顯示「嚴重度未映射」。
- AlertValues 原文，明確標示為「Grafana 告警內容」。
- Validation／normalization warnings，例如「GCP project ID 空白」、「資源未分類」或「規則衝突」。
- RCA `COMPLETE/PARTIAL/FAILED` 的繁體中文狀態與 evidence references。
- AWS 的 `PARTIAL` 報告顯示「目前沒有 AWS MCP 證據」；其他無安全 scope 的報告顯示「目前僅依告警內容分析，尚無可安全查詢的雲端資源範圍」。

前端不自行重新分類、不推定 provider、不解析 AlertValues，也不是 authorization boundary。
前端不提供聊天室、訊息輸入、AI 逐字串流、SSE 或 WebSocket。

## 19. 測試策略與驗收條件

### 19.1 Contract 與 unit tests

- 核准 Grafana body 是 checked-in fixture，未知 fields 可完整保存。
- `resource.label.project_id` 有值為 GCP；不存在為 AWS；存在但空白為 GCP + `VALIDATION_FAILED`。
- `cloud_provider`、DB identifier、Series、ARN 或 folder 不能改變 provider。
- `ERROR -> SEV1`；`WARN/WARNING -> SEV3`；未知值為 `UNMAPPED` warning。
- 任意格式 AlertValues 原文不變，並以 untrusted `alertIssue` 傳給 Agent。
- 多筆 alerts 分開正規化；相同 grouping key 可連結同一 active Incident。
- 正常 identity 精確為 `sourceId + folder + alertname`；invalid fallback 不合併不相關 alert。
- Normalization rule priority、version、conflict、safe condition 與禁止 executable content 皆有測試。
- Prompt injection、惡意 URL、偽造 tool request 不會觸發未授權操作。

### 19.2 PostgreSQL 18 integration tests

- Migration upgrade/downgrade/upgrade 與 schema catalog constraints。
- Exact webhook bytes、JSONB 與 hash round-trip。
- Delivery/alert dedup、Incident identity v2、resolved 行為、closed/reopen relation。
- Valid、UNCLASSIFIED、VALIDATION_FAILED 均建立正確 Incident/RCA/job/outbox。
- Mixed webhook 與 transaction rollback。
- 併發 ingestion 同 identity 只建立一個 active Incident、一個 active RCA、一個 worker job 與一個 outbox idempotency key。
- Nullable legacy scope 與 folder authorization mapping 不造成跨 scope 資料外洩。
- Evidence UUID + partition timestamp reference 可精確讀回 raw evidence。

### 19.3 官方 Pub/Sub Emulator integration tests

- 使用官方 Emulator 與 production Google client library。
- Idempotent 建立 topic/subscription。
- Outbox publish、pull、ack、nack/redelivery、duplicate message。
- DB commit 前不可發布；RCA durable settlement 前不可 ack。
- Worker crash、stale lease、最多 3 attempts、300 秒 deadline。
- 訊息不含 AlertValues、raw payload、credential 或 evidence。

### 19.4 RCA/evaluation tests

- GCP valid scope＋三個 MCP specialists、AWS no-MCP `PARTIAL` 與 GCP 無安全 scope三種路徑。
- 無 MCP scope 仍產生 `PARTIAL` 且不呼叫任何 MCP。
- Specialist timeout 或部分失敗仍保存 evidence 與 PARTIAL report。
- 每個 observed fact 都能追溯 evidence ID/partition timestamp。
- 沒有 evidence 時不產生虛構 root cause。
- 報告與 UI 是繁體中文，技術原文保持不變。

### 19.5 服務目標

- 合法 webhook transaction 與 `202` 在 2 秒內完成。
- Incident 在 commit 後可立即由 REST 查詢，使用者重新整理即可看到。
- 完整 RCA 目標在進入 `QUEUED` 後 300 秒內完成；逾時必須產生 durable PARTIAL/FAILED 狀態。

## 20. 實作分解

本設計跨越兩個有順序依賴的子專案，實作時使用兩份計畫：

1. [`2026-08-13-grafana-normalization-operator-ui-plan.md`](../plans/2026-08-13-grafana-normalization-operator-ui-plan.md)：契約、provider、rules、identity v2、nullable legacy scope、transactional ingestion、Operator REST 與獨立 Angular 顯示。
2. [`2026-08-13-pubsub-emulator-rca-worker-plan.md`](../plans/2026-08-13-pubsub-emulator-rca-worker-plan.md)：官方 Emulator、訊息契約、worker lifecycle、ADK/MCP、evidence 與 report。

第二階段依賴第一階段的 canonical alert、Incident identity v2 與資料庫欄位完成。兩階段都不得加入 production infrastructure provisioning。
