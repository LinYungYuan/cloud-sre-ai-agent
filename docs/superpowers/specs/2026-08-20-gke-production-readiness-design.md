# GKE 正式環境就緒設計

日期：2026-08-20
狀態：已核准，進入實作計畫

## 目標

讓 SRE Agent 的 Backend、Angular Frontend、RCA Worker、outbox publisher、
資料庫 migration 與 partition maintenance workload 具備部署到 Google
Kubernetes Engine 的條件。

本次工作會產出正式環境 container image，以及應用 workload 的 Kustomize
base，但不會建立 Google Cloud 基礎設施。Cloud SQL、Pub/Sub、Workload
Identity 綁定、Secret、Gateway、DNS 與 TLS 都是外部部署輸入，之後可交由
Terraform 管理。

## 範圍

實作範圍包含：

- 建立 Backend 正式環境 image，供 API、outbox publisher、Backend migration
  Job 與 partition maintenance CronJob 共用。
- 建立用來提供編譯後 Angular 應用程式的 Frontend 正式環境 image。
- 建立 RCA Worker 正式環境 image，供 Worker Deployment 與 Worker migration
  Job 共用。
- 新增 Backend liveness 與具備資料庫檢查的 readiness endpoint。
- 為 outbox publisher 建立職責單一且最小化的設定模型。
- 為 Pub/Sub 資源自動建立功能與每個 Pod 的 Worker identity 提供明確設定。
- 建立 Kubernetes Deployment、Service、ConfigMap、ServiceAccount、CronJob
  與依序執行的 migration Job template。
- 補充部署與驗證文件。

實作範圍不包含：

- Cloud SQL 與 Pub/Sub Terraform 資源。
- 建立或填入 Kubernetes Secret。
- Gateway、Ingress、DNS、憑證、網域與外部 load balancer。
- Operator API 身分驗證；非本機環境的 Operator API 會繼續以 HTTP 503
  fail closed。
- Container Registry 與 CI/CD pipeline 實作。
- RCA Worker 水平擴展及以 Pub/Sub backlog 為依據的 HPA。
- 真實 MCP endpoint、capability manifest、模型憑證或正式 evidence 存取。

## 架構

部署後的資料流如下：

```text
Frontend Service ----> Backend Service ----> Cloud SQL
                           |
                           v
                    outbox_events table
                           |
                           v
                  Outbox Deployment ----> Pub/Sub ----> RCA Worker Deployment
                                                            |
                                                            v
                                                     Cloud SQL / MCP / model
```

只有 Frontend 與 Backend workload 具有 Kubernetes Service。Outbox publisher
與 RCA Worker 都是背景 Deployment，不接收對內或對外的應用流量。

同一個 Backend image 會使用不同 command 分別執行 API、outbox publisher、
Backend migration 與 partition maintenance。同一個 RCA Worker image 則供
Worker 與 Worker migration 共用，藉此避免 runtime code 與 schema management
code 在不同 release 間產生版本漂移。

## 元件設計

### Backend

Backend 會新增 multi-stage 正式環境 Dockerfile。相依套件以 repository 內的
`uv.lock` 安裝；runtime 使用 non-root user，並在 port 8000 啟動 Uvicorn。
Uvicorn 會成為鎖定版本的正式相依套件，而不是未宣告的本機工具。

新增兩個不需身分驗證的操作 endpoint，路徑獨立於 `/api/v1` 與 Grafana
webhook namespace：

- `GET /health/live`：ASGI process 能回應 request 時回傳成功。此 endpoint
  不查詢 PostgreSQL，避免暫時性資料庫故障引發重啟迴圈。
- `GET /health/ready`：透過專用 health dependency 執行有 timeout 的
  `SELECT 1`。只有應用程式完成啟動且 PostgreSQL 可連線時才回傳成功。

Health response 不得包含設定值、credential、catalog 內容或 exception text。
Readiness 失敗時只回傳通用的 HTTP 503 response。

正式環境的 Operator 身分驗證維持現狀。當 `APP_ENVIRONMENT` 不是 `local` 時，
既有 unavailable identity provider 會繼續回傳 503。部署不得以設定
`APP_ENVIRONMENT=local` 的方式繞過身分驗證。

### Outbox publisher

Outbox publisher 是 transaction database 與 Pub/Sub 之間必要的 runtime
橋接元件。Backend transaction 先將 event 寫入 `outbox_events`；publisher 再
claim 尚未處理的 row 並發布。缺少這個 workload 時，RCA Worker 不會收到
Backend 建立的任何 job。

Publisher 會改用專用的 `OutboxSettings`，只包含：

- `DATABASE_URL`
- `PUBSUB_PROJECT_ID`
- `RCA_TOPIC_ID`

它不再需要 Grafana credential、模型選擇或 MCP endpoint。Publisher 會使用
Backend image，以沒有 Service 的獨立 Deployment 執行。

### Frontend

Frontend image 以 Node.js 24 作為 build stage，runtime 使用 unprivileged
Nginx 並監聽 port 8080。Nginx 負責：

- 提供 Angular static asset。
- 將 client-side route fallback 到 `index.html`。
- 提供 Kubernetes probe 使用的 `/healthz`。
- 區分快取策略：`index.html` 與 `config.json` 不使用 immutable cache；具有
  hash 的 Angular asset 可以長期快取。

Image 內含安全的預設 `config.json`。Kubernetes 會以 ConfigMap 掛載環境專用
的 `config.json`，因此 `apiBaseUrl`、locale 與 time zone 可以在不重新 build
image 的情況下變更。正式環境 image 的 Nginx 不代理 API；未來的 Gateway
必須將設定的 API path 導向 Backend Service。

### RCA Worker

RCA Worker Dockerfile 的預設 command 會改為啟動 `sre-agent-rca-worker`，不再
只輸出 package version 後結束。Container 使用 non-root user 執行。

Worker settings 新增：

- `PUBSUB_AUTO_CREATE`，預設為 `false`。
- `WORKER_ID`；未明確設定時，預設使用 container hostname。

當 `PUBSUB_AUTO_CREATE=false` 時，Worker 只組合已設定的 topic 與 subscription
path，不呼叫 create API。Terraform 負責 Pub/Sub 資源時，正式環境便可採用
least-privilege IAM。本機開發則設定 `PUBSUB_AUTO_CREATE=true`，保留 Emulator
自動建立資源的行為。

Worker Deployment 初期只使用一個 replica。Pod hostname 會成為唯一的 lease
owner，移除 hard-coded 的共用 identity，也為未來增加 replica 做好 claim
protocol 準備。水平擴展本身不在本次範圍內，必須另做 concurrency 與 load
test。

Worker 沒有 network Service，因此不設定 readiness probe。無法恢復的 startup
或 pull-loop error 必須終止 process，交由 Deployment controller 重啟。Pod 的
termination grace 至少設為 330 秒，以涵蓋設定的 300 秒 RCA deadline。

## 資料庫操作

### Migration

Terraform 負責建立 Cloud SQL instance、database、application role、networking
與 availability 設定。Alembic migration 則在該 database 內建立及演進應用
schema，包括 table、index、foreign key、constraint、version table 與初始
partition。

Migration 分為兩條且必須依序執行：

1. Backend migration 建立及升級共用核心 schema。
2. RCA Worker migration 驗證必要的 Backend revision，再升級 Worker 擁有的
   lifecycle 與 evidence 欄位。

Migration manifest 不放入長時間運行 workload 的 Kustomize base。Release
pipeline 必須建立 Backend migration Job 並等待成功，再建立 Worker migration
Job 並等待成功，最後才能套用或 rollout 應用程式 base。任何 migration 失敗
都會停止 release。

Migration 不會新增環境專用的 Grafana source、normalization rule、user 或
scope grant。這些資料需要另外的受控 seed 或管理流程，不屬於本設計。

### Partition maintenance

數個高流量 table 使用 PostgreSQL 月份 partition。Migration 只建立初始的
partition runway。Partition command 會維持本月與接下來兩個月的 partition，
避免月份切換時因沒有符合的 partition 而使 insert 失敗。

Kubernetes CronJob 每日執行既有 partition maintenance command。CronJob 使用
`concurrencyPolicy: Forbid`，設定有限的 retry 次數與 deadline，並只保留少量
成功及失敗 Job history 供維運調查。

## Kubernetes 資源

Repository 會新增以下結構：

```text
deploy/k8s/
  README.md
  base/
    kustomization.yaml
    configmap.yaml
    serviceaccounts.yaml
    backend-deployment.yaml
    backend-service.yaml
    frontend-deployment.yaml
    frontend-service.yaml
    outbox-deployment.yaml
    worker-deployment.yaml
    partition-cronjob.yaml
  jobs/
    backend-migration-job.yaml
    worker-migration-job.yaml
```

Base 會定義資源名稱與 reference，但不指定 namespace、網域、image registry、
Google service account annotation 或 Secret data。這些都屬於部署環境設定，
之後可由 overlay、Terraform 或 release tooling 提供。

所有 Pod 都使用 non-root security context、停用 privilege escalation、移除
Linux capability，並在相容時使用 read-only root filesystem；同時設定 CPU
與 memory request／limit。需要寫入的暫存路徑會以 `emptyDir` volume 提供。

ConfigMap 只包含非敏感預設值，例如：

- `APP_ENVIRONMENT=production`
- 可被環境覆寫的 Pub/Sub project、topic 與 subscription identifier。
- Model name 與經核准 HTTPS MCP endpoint 的安全預設值。
- `PUBSUB_AUTO_CREATE=false`。
- Frontend runtime configuration。

Workload 會引用預先存在的 `sre-agent-secrets` Secret 中的個別 key。Backend 與
資料庫相關 workload 至少需要 `DATABASE_URL`，Backend 另需要
`GRAFANA_TOKENS`。任何 Secret value 都不得提交到 repository。

ServiceAccount 只定義 Kubernetes identity。Workload Identity 綁定與 IAM role
維持為外部設定。預期權限拆分如下：

- Outbox ServiceAccount：對 RCA topic 的 Pub/Sub publisher 權限。
- Worker ServiceAccount：對 RCA subscription 的 Pub/Sub subscriber 權限，以及
  所選正式整合需要的 model／MCP 存取權。
- Backend 與 migration ServiceAccount：Cloud SQL 連線能力，以及
  `DATABASE_URL` 所代表的資料庫權限。

## Rollout 與故障處理

Release 順序如下：

```text
Terraform 建立的 dependency 已可使用
  -> 套用 Kubernetes ServiceAccount
  -> Backend migration Job 成功
  -> Worker migration Job 成功
  -> 套用完整 base（再次套用 ServiceAccount 是冪等操作）
  -> Backend readiness 成功
  -> 外部 routing 可開始送入流量
```

ServiceAccount 必須先於 migration Job 存在，否則 Job Pod 無法排程。Secret 仍由
外部流程預先建立；migration Job 不會建立或修改 Secret。

Backend rolling update 依靠 readiness 將不健康的 Pod 從 Service endpoint
移除。Liveness 刻意不依賴 PostgreSQL。

Outbox 與 Worker failure 會保留既有的資料庫及 Pub/Sub retry semantic。只有
process boundary 結束後 Kubernetes 才會重啟程序；manifest 不會以 shell retry
loop 隱藏 fatal error。

Worker 維持一個 replica。Outbox publisher 初期也使用一個 replica；既有的
`FOR UPDATE SKIP LOCKED` claim 行為為未來 scaling review 保留空間，但本次
release 不包含擴展。

## 測試與驗證

會以 test-driven development 實作行為變更。必要驗證包含：

- Backend liveness、readiness 成功，以及 readiness failure 不洩漏資訊的測試。
- 證明 `OutboxSettings` 只接受必要 runtime input 的 unit test。
- Worker production no-create、本機 auto-create，以及由 hostname 產生 Worker
  identity 的測試。
- 既有 Backend、RCA Worker、contract 與 Frontend 的 test、type 與 lint gate。
- 三個 production container image 的 build。
- Kustomize application base render 與 manifest 靜態驗證。
- Backend health、Frontend `/healthz`／SPA fallback，以及 Worker process startup
  行為的 container smoke test。
- 使用 PostgreSQL 與 Pub/Sub Emulator 的既有本機 Grafana webhook 端到端
  路徑，確認 outbox publishing 與 Worker processing 沒有 regression。

任何驗證步驟都不得建立、修改或刪除 Google Cloud 資源。

## 驗收條件

符合以下條件時才算完成：

- Backend、Frontend 與 RCA Worker production image 都能以 checked-in lock
  file 重現 build。
- 每個 image 都以 non-root user 啟動預期的 production process。
- Backend liveness 與 database-aware readiness 符合設計行為。
- Frontend production image 能提供 runtime configuration、client-side route
  與 health check。
- Production Worker 啟動時不需要 Pub/Sub resource creation 權限，並使用唯一
  Worker identity。
- Outbox 使用職責單一的專用 settings。
- Kustomize 能 render 所有長時間運行的應用資源與 partition CronJob，且不包含
  Secret data 或基礎設施資源。
- 已提供依序執行的 migration Job template 與操作說明。
- 所有必要自動化及 smoke verification 均通過。
- Operator authentication、Terraform 資源、external routing 與 Secret value
  明確維持在交付範圍之外。
