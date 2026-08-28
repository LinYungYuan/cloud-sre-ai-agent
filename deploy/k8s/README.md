# GKE 發布操作手冊

`deploy/k8s/base` 是可攜式應用程式 base。它刻意不指定 namespace 或 registry，
也不包含 Google service account annotation、Secret 資料、Gateway resource 或
Terraform 管理的雲端資源。各環境的發布流程必須自行提供這些輸入。

## 發布輸入與事前檢查

執行發布前，請確認 Terraform 或其他基礎設施流程已提供 Cloud SQL、RCA Pub/Sub
topic 與 subscription、網路，以及必要的 Workload Identity 綁定。發布工具也必須
準備該環境的 namespace、Gateway、registry image 與既有的
`sre-agent-secrets` Secret。

提交至儲存庫的 manifest 使用以 `:latest` 結尾的本機 image 名稱，但這些名稱只作為
Kustomize transformation key。建立任一 migration Job 前，發布工具必須對 base 與
Job manifest 套用 Kustomize `images` override，並產生使用不可變 digest 的發布
bundle，例如 `REGISTRY/sre-agent-backend@sha256:...`。不得部署儲存庫中的
`:latest` reference。以下依序執行的命令假設這些路徑都指向已產生的發布 bundle。

請先選擇目標 cluster context 與 namespace。空白 namespace 代表 context 將使用
`default`；除非 `default` 就是明確指定的目標，否則不得繼續。

```bash
kubectl config current-context
TARGET_NAMESPACE=$(kubectl config view --minify -o jsonpath='{..namespace}')
test -n "${TARGET_NAMESPACE}"
kubectl get namespace "${TARGET_NAMESPACE}"
kubectl get secret sre-agent-secrets
```

既有的 `sre-agent-secrets` 必須包含以下 key；不得列印或提交其值：

- `DATABASE_URL`：兩條 migration、Backend 與 Worker 共用的 PostgreSQL connection URL。
- `GRAFANA_TOKENS`：Backend 使用的 Grafana Bearer Token catalog。

KSA-to-GSA 綁定與 IAM role 由環境負責。請分離以下預期職責：

- `sre-agent-backend`：供 Backend 與 Backend migration 使用的 Cloud SQL 連線能力，
  以及 `DATABASE_URL` 所代表的資料庫權限，以及 RCA Pub/Sub topic 的發布權限。
- `sre-agent-rca-worker`：RCA subscription 的訂閱權限，以及正式環境整合所核准的
  model／MCP 存取權。Worker migration 會使用此 KSA，因此環境也必須允許該
  one-shot Job 存取 `DATABASE_URL` 所代表的資料庫。

## 依序發布

請從已產生的發布 bundle 根目錄執行以下命令。此順序具有約束力：任何命令失敗都必須
立即停止。Backend migration 完成前不得建立 Worker migration；兩條 migration
都完成前不得套用完整 base。

```bash
set -euo pipefail
kubectl apply -f deploy/k8s/base/serviceaccounts.yaml
BACKEND_JOB=$(kubectl create -f deploy/k8s/jobs/backend-migration-job.yaml -o jsonpath='{.metadata.name}')
kubectl wait --for=condition=complete --timeout=15m "job/${BACKEND_JOB}"
WORKER_JOB=$(kubectl create -f deploy/k8s/jobs/worker-migration-job.yaml -o jsonpath='{.metadata.name}')
kubectl wait --for=condition=complete --timeout=15m "job/${WORKER_JOB}"
kubectl apply -k deploy/k8s/base
kubectl rollout restart deployment/sre-agent-backend
kubectl rollout restart deployment/sre-agent-frontend
kubectl rollout restart deployment/sre-agent-rca-worker
kubectl rollout status deployment/sre-agent-backend --timeout=5m
kubectl rollout status deployment/sre-agent-frontend --timeout=5m
kubectl rollout status deployment/sre-agent-rca-worker --timeout=5m
```

每個 migration template 都使用 `generateName`，因此每次發布都會建立獨立且不可變的
Job 紀錄。Kubernetes 會保留已完成的 Job 24 小時。若 Job 失敗，請先檢查，再建立
新的 Job 重試：

```bash
kubectl describe "job/${BACKEND_JOB}"
kubectl logs "job/${BACKEND_JOB}"
kubectl describe "job/${WORKER_JOB}"
kubectl logs "job/${WORKER_JOB}"
```

## 路由與正式環境限制

外部 Gateway 必須將 `/api` 與 `/webhooks/v1/grafana` 都路由至 port 8000 的
`sre-agent-backend` Service，並將 Frontend 流量路由至 port 8080 的
`sre-agent-frontend`。Gateway、DNS、TLS 與 Load Balancer 設定不屬於此可攜式
base 的範圍。

正式環境尚未實作 Operator API 身分驗證。當 `APP_ENVIRONMENT=production` 時，
Operator API request 會持續以 HTTP 503 fail closed；不得將環境設為 `local` 來繞過
此控制。

## 回滾

應用程式回滾可以還原上一版不可變 image digest 與 manifest，接著執行相同的 rollout
status 檢查。不得自動 downgrade 資料庫：schema downgrade 可能破壞資料，或使兩條
migration stream 不一致。migration 執行後，請使用經過 review 的 forward-fix
migration，或另行核准的資料庫復原程序。
