# GKE 正式環境就緒實作計畫

> **給自動化實作者：** 必須使用 `superpowers:subagent-driven-development`（建議）或 `superpowers:executing-plans`，逐項執行本計畫。每個任務採 TDD、完成驗證後才提交。

**目標：** 補齊 Backend、Frontend、RCA Worker、outbox publisher、migration 與 partition maintenance 的正式 container 與 GKE application manifests；不建立 PostgreSQL、Pub/Sub 或其他 Google Cloud 基礎設施。

**架構：** Backend image 供 API、outbox、Backend migration 與 partition maintenance 共用；RCA Worker image 供 Worker 與 Worker migration 共用；Frontend image 由 unprivileged Nginx 提供 Angular static files。Kustomize base 只描述應用 workload，Secret、Workload Identity 綁定、Gateway 與 Cloud 資源是外部輸入。

**技術棧：** Python 3.12、uv、FastAPI、SQLAlchemy async、Alembic、Angular、Node.js 24、unprivileged Nginx、Docker、Kubernetes、Kustomize、pytest。

**規格：** `docs/superpowers/specs/2026-08-20-gke-production-readiness-design.md`

## 全域限制

- 不新增 Operator authentication；正式環境 Operator API 繼續 fail closed 回傳 503。
- 不建立 Cloud SQL、Pub/Sub、Secret、Gateway、DNS、TLS、registry、CI/CD 或 Worker HPA。
- 不提交 Secret value、Google service-account key 或環境專用 credential。
- `PUBSUB_AUTO_CREATE=false` 是正式預設；只有本機 Emulator 明確設為 `true`。
- 所有 image 與 Pod 以 non-root 執行，停用 privilege escalation，移除 Linux capabilities，並設定 resources。
- Worker 初期固定一個 replica，`terminationGracePeriodSeconds >= 330`。
- Migration 順序固定為 ServiceAccount、Backend Job、Worker Job、完整 base。
- 保留目前工作樹中使用者既有修改；每次提交前只 stage 該任務列出的檔案並檢查 staged diff。

## 檔案責任

- Backend runtime：`backend/src/sre_agent/`、`backend/tests/`、`backend/Dockerfile`。
- Worker runtime：`rca-worker/src/sre_rca_worker/`、`rca-worker/tests/`、`rca-worker/Dockerfile`。
- Frontend runtime：`frontend/Dockerfile`、`frontend/nginx.conf`、`frontend/README.md`。
- GKE manifests：`deploy/k8s/` 與 `contracts/compatibility-tests/test_gke_manifests.py`。
- 操作說明：根目錄 `README.md` 與 `deploy/k8s/README.md`。

---

### 任務 1：Backend liveness 與 database-aware readiness

**檔案：**
- 建立：`backend/src/sre_agent/api/routers/health.py`
- 建立：`backend/tests/contract/api/test_health.py`
- 修改：`backend/src/sre_agent/api/composition.py`
- 修改：`backend/src/sre_agent/api/dependencies.py`
- 修改：`backend/src/sre_agent/api/main.py`
- 修改：`backend/tests/contract/api/test_app_composition.py`
- 修改：`backend/tests/integration/api/test_production_app_composition.py`

**介面：**

`ReadinessCheck` 的精確型別是 `Callable[[], Awaitable[None]]`。Router 暴露
`async def live() -> dict[str, str]` 與
`async def ready(check: ReadinessCheck = Depends(get_readiness_check)) -> Response`。

- [ ] **步驟 1：先寫 RED contract tests**

測試 `/health/live` 回傳 `200 {"status":"ok"}` 且完全不呼叫 readiness dependency；測試 `/health/ready` 成功回傳 200；讓 check 拋出 `RuntimeError("postgresql://secret")` 時，確認只回傳 `503 {"detail":"service unavailable"}`，body 不含 exception text。

- [ ] **步驟 2：執行 RED**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/contract/api/test_health.py -v`

預期：因 router 與 dependency 尚不存在而 FAIL。

- [ ] **步驟 3：實作無循環 import 的 composition**

在 `composition.py` 從 `collections.abc` import `Awaitable`，並在 `UnitOfWorkFactory` 旁定義 `ReadinessCheck`。將 `readiness_check` 加到 `RuntimeResources` 與 `ApplicationServices`。`dependencies.py` 從 composition import `ApplicationServices, ReadinessCheck`，新增：

```python
def get_readiness_check(request: Request) -> ReadinessCheck:
    return _services(request).readiness_check
```

不得讓 `composition.py` 反向 import `dependencies.py`。

- [ ] **步驟 4：接上 production SQL check**

在 `production_resources()` 建立 closure，透過既有 engine 執行 `await connection.execute(text("SELECT 1"))`。在 health router 用 `asyncio.wait_for(check(), timeout=2.0)`；捕捉 timeout 與一般 exception 後回傳固定 503，不記錄 URL 或 credential。把 router include 到 app。

- [ ] **步驟 5：驗證 composition 與 endpoints**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/contract/api/test_health.py tests/contract/api/test_app_composition.py tests/integration/api/test_production_app_composition.py -v`

預期：全部 PASS，production composition test 證明 ready check 實際執行 SQL；live test 證明 DB failure 不影響 liveness。

- [ ] **步驟 6：提交**

```bash
git add -- backend/src/sre_agent/api/routers/health.py backend/src/sre_agent/api/composition.py backend/src/sre_agent/api/dependencies.py backend/src/sre_agent/api/main.py backend/tests/contract/api/test_health.py backend/tests/contract/api/test_app_composition.py backend/tests/integration/api/test_production_app_composition.py
git diff --cached --check
git commit -m "feat(backend): add Kubernetes health probes"
```

---

### 任務 2：隔離 Outbox publisher 設定

**檔案：**
- 建立：`backend/src/sre_agent/config/outbox_settings.py`
- 建立：`backend/tests/unit/config/test_outbox_settings.py`
- 修改：`backend/src/sre_agent/workers/outbox_main.py`

**介面：**

`OutboxSettings(BaseSettings)` 只有 `database_url: SecretStr`、
`pubsub_project_id: str` 與 `rca_topic_id: str`。`run_outbox_worker` 保留 keyword-only
`poll_seconds: float = 1.0`，並新增 keyword-only
`settings_factory: Callable[[], OutboxSettings] = _load_outbox_settings`。

- [ ] **步驟 1：先寫 settings RED tests**

使用 `model_validate` 驗證三個必要欄位可建立設定；缺少任一欄位會失敗；加入 `GRAFANA_TOKENS`、`MODEL_NAME` 或 MCP URL 會因 `extra="forbid"` 失敗；錯誤訊息不得顯示 database password。

- [ ] **步驟 2：執行 RED**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/config/test_outbox_settings.py -v`

預期：module 尚不存在而 FAIL。

- [ ] **步驟 3：實作專用設定與注入點**

沿用 `SettingsConfigDict(env_file=None, extra="forbid", hide_input_in_errors=True)` 與 PostgreSQL URL validator。新增 `_load_outbox_settings()` 包裝無參數建構，避免把 Pydantic class 本身當作 callback 型別。修改 `outbox_main.py` 僅讀取專用設定；publisher loop 與 process boundary 行為不變。

- [ ] **步驟 4：驗證**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/config/test_outbox_settings.py tests/unit/workers/test_outbox_main.py -v`

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pyright src/sre_agent/config/outbox_settings.py src/sre_agent/workers/outbox_main.py`

- [ ] **步驟 5：提交**

```bash
git add -- backend/src/sre_agent/config/outbox_settings.py backend/src/sre_agent/workers/outbox_main.py backend/tests/unit/config/test_outbox_settings.py
git diff --cached --check
git commit -m "refactor(backend): isolate outbox runtime settings"
```

---

### 任務 3：RCA Worker identity 與 Pub/Sub no-create mode

**檔案：**
- 修改：`rca-worker/src/sre_rca_worker/config/settings.py`
- 修改：`rca-worker/src/sre_rca_worker/integrations/pubsub/bootstrap.py`
- 修改：`rca-worker/src/sre_rca_worker/workers/rca_worker.py`
- 修改：`rca-worker/tests/unit/config/test_settings.py`
- 修改：`rca-worker/tests/unit/integrations/pubsub/test_bootstrap.py`
- 修改：`rca-worker/tests/integration/workers/test_rca_worker.py`
- 修改：`rca-worker/README.md`
- 修改：`README.md`

**介面：**

`WorkerSettings` 新增 `pubsub_auto_create: bool = False`。新增
`_default_worker_id() -> str`，在函式執行時呼叫 `socket.gethostname()`；
`worker_id: str = Field(default_factory=_default_worker_id, min_length=1)`，並以
field validator 拒絕只含空白的值。
`prepare_topic_and_subscription` 保留現有 publisher、subscriber、project/topic/
subscription keyword 參數，新增必要的 keyword-only `auto_create: bool`，回傳
`tuple[str, str]`。

- [ ] **步驟 1：寫 RED tests**

確認 default `pubsub_auto_create` 是 false；沒有 `WORKER_ID` 時以 monkeypatch 後 hostname 建立 identity；空白 identity 被拒絕。Bootstrap 在 false 時只呼叫 `topic_path` 與 `subscription_path`，不呼叫 create API；true 時保留 AlreadyExists-safe 建立行為。Integration test 以 monkeypatch 攔截 `RcaJobHandler` constructor，確認收到的 `worker_id` 等於 `settings.worker_id`。

- [ ] **步驟 2：執行 RED**

Run: `cd rca-worker && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/config/test_settings.py tests/unit/integrations/pubsub/test_bootstrap.py tests/integration/workers/test_rca_worker.py -v`

- [ ] **步驟 3：實作並保持本機相容**

新增 `prepare_topic_and_subscription`；既有 `ensure_topic_and_subscription` 可保留為 `auto_create=True` 的相容 wrapper。Production runner 傳入設定值並將 hard-coded `"rca-worker"` 改為 `settings.worker_id`。文件中的 Emulator 啟動範例明確加入 `PUBSUB_AUTO_CREATE=true`；正式範例維持 false。

- [ ] **步驟 4：驗證**

重跑步驟 2，另執行：`cd rca-worker && UV_CACHE_DIR=$PWD/.uv-cache uv run pyright src tests`

- [ ] **步驟 5：提交**

```bash
git add -- rca-worker/src/sre_rca_worker/config/settings.py rca-worker/src/sre_rca_worker/integrations/pubsub/bootstrap.py rca-worker/src/sre_rca_worker/workers/rca_worker.py rca-worker/tests/unit/config/test_settings.py rca-worker/tests/unit/integrations/pubsub/test_bootstrap.py rca-worker/tests/integration/workers/test_rca_worker.py rca-worker/README.md README.md
git diff --cached --check
git commit -m "feat(worker): support production Pub/Sub resources"
```

---

### 任務 4：Backend 正式環境 image

**檔案：**
- 建立：`backend/Dockerfile`
- 建立：`backend/.dockerignore`
- 修改：`backend/pyproject.toml`
- 修改：`backend/uv.lock`
- 修改：`backend/README.md`

**Container contract：** Python 3.12.9 multi-stage build；以鎖定的 uv 0.11.6 安裝 frozen dependencies；runtime user/group 使用 numeric UID/GID 65532；port 8000；預設 command 為 `uvicorn sre_agent.api.main:app --host 0.0.0.0 --port 8000`；image 必須包含 `migrations/` 與 `alembic.ini`。

- [ ] **步驟 1：先建立會失敗的 image contract check**

Run: `test -f backend/Dockerfile && rg 'USER 65532:65532|USER 65532' backend/Dockerfile && rg 'uvicorn sre_agent.api.main:app' backend/Dockerfile`

預期：Dockerfile 尚不存在而 FAIL。

- [ ] **步驟 2：鎖定正式 server dependency**

將 `uvicorn` 加入 Backend production dependencies，執行：`cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv lock`。確認 lock diff 只有 dependency resolution 的必要變更。

- [ ] **步驟 3：實作 multi-stage image**

Builder 先複製 `pyproject.toml`、`uv.lock` 安裝 locked dependencies，再複製 source；runtime 只帶 `.venv`、`src`、`migrations`、`alembic.ini`。設定 `PYTHONDONTWRITEBYTECODE=1`、`PYTHONUNBUFFERED=1`、`PATH=/app/.venv/bin:$PATH`，以 65532 執行。`.dockerignore` 排除 `.venv`、cache、tests、coverage、`.env`、git metadata。

- [ ] **步驟 4：build 與 metadata smoke test**

Run: `docker build -t sre-agent-backend:gke-plan backend`

Run: `docker image inspect sre-agent-backend:gke-plan --format '{{json .Config.User}} {{json .Config.Cmd}} {{json .Config.ExposedPorts}}'`

預期：user 是 `65532:65532` 或 `65532`，command 啟動 Uvicorn，port 是 8000。

Run: `docker run --rm --entrypoint alembic sre-agent-backend:gke-plan --help`

預期：exit 0，證明 migration runtime 存在。

- [ ] **步驟 5：提交**

```bash
git add -- backend/Dockerfile backend/.dockerignore backend/pyproject.toml backend/uv.lock backend/README.md
git diff --cached --check
git commit -m "build(backend): add production container image"
```

---

### 任務 5：RCA Worker 正式環境 image

**檔案：**
- 修改：`rca-worker/Dockerfile`
- 建立：`rca-worker/.dockerignore`
- 修改：`rca-worker/README.md`

**Container contract：** Python 3.12.9 multi-stage build；frozen Worker lock；numeric UID/GID 65532；預設 command 啟動 `sre-agent-rca-worker`；包含 Worker migrations 與 Alembic config。

- [ ] **步驟 1：先證明現有 command 不符合正式用途**

Run: `rg 'sre-agent-rca-worker' rca-worker/Dockerfile`

預期：找不到實際 Worker 啟動 command，check FAIL。

- [ ] **步驟 2：重構 multi-stage image**

沿用任務 4 的 reproducible/non-root 原則，但只複製 Worker package、獨立 `.venv`、`migrations` 與 `alembic.ini`。不得 import 或複製 `backend/src`。`.dockerignore` 排除本機 venv、tests、cache、`.env`、git metadata。

- [ ] **步驟 3：build 與 metadata smoke test**

Run: `docker build -t sre-agent-rca-worker:gke-plan rca-worker`

Run: `docker image inspect sre-agent-rca-worker:gke-plan --format '{{json .Config.User}} {{json .Config.Cmd}}'`

預期：non-root numeric user，command 是 `sre-agent-rca-worker`。

Run: `docker run --rm --entrypoint alembic sre-agent-rca-worker:gke-plan --help`

預期：exit 0。

- [ ] **步驟 4：提交**

```bash
git add -- rca-worker/Dockerfile rca-worker/.dockerignore rca-worker/README.md
git diff --cached --check
git commit -m "build(worker): run RCA worker in production image"
```

---

### 任務 6：Frontend unprivileged Nginx image

**檔案：**
- 建立：`frontend/Dockerfile`
- 建立：`frontend/.dockerignore`
- 建立：`frontend/nginx.conf`
- 修改：`frontend/README.md`

**Container contract：** Node.js 24 build stage 執行 `npm ci` 與 production build；runtime 使用 `nginxinc/nginx-unprivileged:1.29.4-alpine`，監聽 8080；提供 `/healthz`、SPA fallback 與分層 cache headers。

- [ ] **步驟 1：先寫 Nginx contract RED check**

Run: `test -f frontend/nginx.conf && rg 'location = /healthz' frontend/nginx.conf && rg 'try_files.*index.html' frontend/nginx.conf`

預期：檔案尚不存在而 FAIL。

- [ ] **步驟 2：實作 production build 與 serving config**

Builder 使用 `node:24.19.0-alpine`，先複製 package lock 再 `npm ci`，執行 `npm run build`。依 Angular output 實際路徑複製 `dist/frontend/browser/`。Nginx config 必須：

```nginx
listen 8080;
location = /healthz { access_log off; return 200 "ok\n"; }
location = /index.html { add_header Cache-Control "no-store" always; }
location = /config.json { add_header Cache-Control "no-store" always; }
location / { try_files $uri $uri/ /index.html; }
```

只對 production build 產生的 hashed JS/CSS/font assets 設一年 immutable cache。Runtime 不代理 `/api`。為 read-only root filesystem 預留 `/tmp`、`/var/cache/nginx` 與 `/var/run` 的 writable volume 契約，並在 README 記錄。

- [ ] **步驟 3：build 與 container smoke test**

Run: `docker build -t sre-agent-frontend:gke-plan frontend`

Run: `docker run --rm -d --name sre-agent-frontend-gke-plan -p 18080:8080 sre-agent-frontend:gke-plan`

Run: `curl --fail --silent http://127.0.0.1:18080/healthz`

Run: `curl --fail --silent http://127.0.0.1:18080/incidents/example | rg '<app-root|<html'`

Run: `docker rm -f sre-agent-frontend-gke-plan`

預期：health 回 `ok`，client-side route 回 Angular shell。最後一個命令清除這個明確命名的暫時 container。

- [ ] **步驟 4：提交**

```bash
git add -- frontend/Dockerfile frontend/.dockerignore frontend/nginx.conf frontend/README.md
git diff --cached --check
git commit -m "build(frontend): add unprivileged production image"
```

---

### 任務 7：建立 Kustomize application base

**檔案：**
- 建立：`deploy/k8s/base/kustomization.yaml`
- 建立：`deploy/k8s/base/configmap.yaml`
- 建立：`deploy/k8s/base/serviceaccounts.yaml`
- 建立：`deploy/k8s/base/backend-deployment.yaml`
- 建立：`deploy/k8s/base/backend-service.yaml`
- 建立：`deploy/k8s/base/frontend-deployment.yaml`
- 建立：`deploy/k8s/base/frontend-service.yaml`
- 建立：`deploy/k8s/base/outbox-deployment.yaml`
- 建立：`deploy/k8s/base/worker-deployment.yaml`
- 建立：`deploy/k8s/base/partition-cronjob.yaml`
- 建立：`contracts/compatibility-tests/test_gke_manifests.py`

**固定名稱與映射：**

| 資源 | 名稱／值 |
|---|---|
| images | `sre-agent-backend:latest`、`sre-agent-frontend:latest`、`sre-agent-rca-worker:latest` |
| ServiceAccounts | `sre-agent-backend`、`sre-agent-outbox`、`sre-agent-rca-worker` |
| Secret | 既有 `sre-agent-secrets`，keys 為 `DATABASE_URL`、`GRAFANA_TOKENS` |
| Services | `sre-agent-backend:8000`、`sre-agent-frontend:8080` |
| ConfigMap | `sre-agent-config`；Frontend runtime JSON key 為 `config.json` |
| Pub/Sub defaults | project `sre-agent`、topic `rca-jobs`、subscription `rca-jobs`、auto-create `false` |
| Frontend config | `apiBaseUrl=/api/v1`、`locale=zh-TW`、`timeZone=Asia/Taipei` |

**Resource contract：**

| workload | requests | limits |
|---|---|---|
| Backend | `cpu: 100m`、`memory: 256Mi` | `cpu: 1000m`、`memory: 1Gi` |
| Frontend | `cpu: 25m`、`memory: 64Mi` | `cpu: 250m`、`memory: 256Mi` |
| Outbox | `cpu: 50m`、`memory: 128Mi` | `cpu: 500m`、`memory: 512Mi` |
| RCA Worker | `cpu: 250m`、`memory: 512Mi` | `cpu: 2000m`、`memory: 2Gi` |
| Partition | `cpu: 50m`、`memory: 128Mi` | `cpu: 500m`、`memory: 512Mi` |

- [ ] **步驟 1：先寫 manifest RED tests**

用 `yaml.safe_load_all` 解析 base 中所有 YAML，測試：

1. `kustomization.yaml` 能引用其餘九個 resource manifests。
2. 恰有四個 Deployment、兩個 Service、一個 CronJob、三個 ServiceAccount，且沒有 Secret、Ingress、Gateway 或 Google Cloud CRD。
3. 每個 container 都有 non-root Pod/container security context、`allowPrivilegeEscalation: false`、`capabilities.drop: [ALL]`、CPU/memory requests 與 limits。
4. Backend 有 live/ready HTTP probes；Frontend 有 `/healthz` probes；Outbox 與 Worker 沒有 Service。
5. Worker `replicas: 1`、`terminationGracePeriodSeconds >= 330`，`WORKER_ID` 由 `fieldRef: metadata.name` 注入。
6. Partition CronJob 每日 `17 2 * * *`、`concurrencyPolicy: Forbid`、有限 history/deadline/retry。
7. ConfigMap 不含 `DATABASE_URL`、Grafana token 或 credential；production auto-create 為 false。

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest ../contracts/compatibility-tests/test_gke_manifests.py -v`

預期：base 尚不存在而 FAIL。

- [ ] **步驟 2：建立 ConfigMap 與 ServiceAccounts**

ConfigMap 放 Backend/Worker 共用的非敏感設定：`APP_ENVIRONMENT=production`、Pub/Sub identifiers、`MODEL_NAME=gemini-2.5-flash`、三個規格已核准的 HTTPS MCP URLs、`MCP_CAPABILITY_MANIFEST=[]`、`PUBSUB_AUTO_CREATE=false`，以及完整 JSON 格式的 `config.json`。不得在 ConfigMap 放 Secret placeholder。

建立三個無 Google service-account annotation 的 KSA；annotation 由未來 overlay/Terraform 管理。

- [ ] **步驟 3：建立 Backend 與 Frontend workloads**

Backend Deployment 使用 2 replicas、`Recreate` 以外的 rolling update、backend KSA、port 8000、ConfigMap env、Secret 的 `DATABASE_URL`/`GRAFANA_TOKENS`，並設定 `/health/live` liveness、`/health/ready` readiness。Backend Service 是 ClusterIP 8000。

Frontend Deployment 使用 2 replicas、port 8080，將 ConfigMap `config.json` 以 `subPath` 掛載到 `/usr/share/nginx/html/config.json`；為 `/tmp`、`/var/cache/nginx`、`/var/run` 配置 `emptyDir`，使 root filesystem 可 read-only。Frontend Service 是 ClusterIP 8080。

- [ ] **步驟 4：建立背景 workloads**

Outbox Deployment 使用 1 replica、outbox KSA、Backend image，command `sre-agent-outbox-worker`，只注入 `DATABASE_URL`、`PUBSUB_PROJECT_ID`、`RCA_TOPIC_ID`。

Worker Deployment 使用 1 replica、worker KSA、Worker image，command `sre-agent-rca-worker`，注入所有 Worker settings；`WORKER_ID` 使用 Pod metadata.name；termination grace 330；不建立 port、Service 或 HTTP probe。

Partition CronJob 使用 Backend image，command `sre-agent-ensure-partitions`，只注入 `DATABASE_URL`；schedule `17 2 * * *`、`concurrencyPolicy: Forbid`、`startingDeadlineSeconds: 1800`、`backoffLimit: 2`、成功/失敗 history 各 2。

- [ ] **步驟 5：render 與 contract verification**

Run: `kubectl kustomize deploy/k8s/base > /tmp/sre-agent-gke-base.yaml`

Run: `kubectl apply --dry-run=client -f /tmp/sre-agent-gke-base.yaml`

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest ../contracts/compatibility-tests/test_gke_manifests.py -v`

預期：全部 PASS，rendered YAML 不含 Secret object 或空白 image name。

- [ ] **步驟 6：提交**

```bash
git add -- deploy/k8s/base contracts/compatibility-tests/test_gke_manifests.py
git diff --cached --check
git commit -m "feat(deploy): add GKE application base"
```

---

### 任務 8：Migration Jobs 與發布操作文件

**檔案：**
- 建立：`deploy/k8s/jobs/backend-migration-job.yaml`
- 建立：`deploy/k8s/jobs/worker-migration-job.yaml`
- 建立：`deploy/k8s/README.md`
- 修改：`contracts/compatibility-tests/test_gke_manifests.py`
- 修改：`README.md`

**Job contract：** 使用 `generateName` 產生不可變的每次發布 Job；Backend Job command 為 `alembic upgrade head`；Worker Job command 相同但使用 Worker image；兩者只讀既有 `sre-agent-secrets/DATABASE_URL`，設定 `restartPolicy: Never`、`backoffLimit: 0` 與有限 `activeDeadlineSeconds`。

- [ ] **步驟 1：先擴充 RED tests**

測試兩個 template 都是 `batch/v1 Job`、使用 `generateName` 而非固定 `metadata.name`、沒有 Secret data、使用正確 image/KSA、只注入 `DATABASE_URL`、non-root/security/resources 完整、`backoffLimit: 0`，且 Worker Job 不會先於 Backend Job 被文件指示執行。

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest ../contracts/compatibility-tests/test_gke_manifests.py -v`

預期：Job template 尚不存在而 FAIL。

- [ ] **步驟 2：建立 Job templates**

Backend Job 使用 `sre-agent-backend:latest`、KSA `sre-agent-backend`，command `alembic upgrade head`。Worker Job 使用 `sre-agent-rca-worker:latest`、KSA `sre-agent-rca-worker`，command `alembic upgrade head`。兩者設定 `activeDeadlineSeconds: 900`、`ttlSecondsAfterFinished: 86400`、`backoffLimit: 0`，requests 為 `cpu: 100m`／`memory: 256Mi`，limits 為 `cpu: 1000m`／`memory: 1Gi`，並套用與 base 相同的 security 原則。

- [ ] **步驟 3：撰寫可直接執行的 release runbook**

文件先要求確認 namespace 與既有 `sre-agent-secrets`，再依序執行：

```bash
kubectl apply -f deploy/k8s/base/serviceaccounts.yaml
BACKEND_JOB=$(kubectl create -f deploy/k8s/jobs/backend-migration-job.yaml -o jsonpath='{.metadata.name}')
kubectl wait --for=condition=complete --timeout=15m "job/${BACKEND_JOB}"
WORKER_JOB=$(kubectl create -f deploy/k8s/jobs/worker-migration-job.yaml -o jsonpath='{.metadata.name}')
kubectl wait --for=condition=complete --timeout=15m "job/${WORKER_JOB}"
kubectl apply -k deploy/k8s/base
kubectl rollout status deployment/sre-agent-backend --timeout=5m
kubectl rollout status deployment/sre-agent-frontend --timeout=5m
kubectl rollout status deployment/sre-agent-outbox --timeout=5m
kubectl rollout status deployment/sre-agent-rca-worker --timeout=5m
```

同一節說明 image 必須先由 release tool 以 Kustomize `images` 覆寫成 immutable digest；列出 Secret keys、三個 KSA 的預期 IAM 分工、Gateway 必須將 `/api` 導到 Backend；說明 Operator API production 仍回 503；rollback 只能回滾 image/manifests，不可自動 downgrade database。

- [ ] **步驟 4：驗證 YAML 與文件命令**

Run: `kubectl create --dry-run=client -f deploy/k8s/jobs/backend-migration-job.yaml -o yaml > /tmp/sre-agent-backend-migration.yaml`

Run: `kubectl create --dry-run=client -f deploy/k8s/jobs/worker-migration-job.yaml -o yaml > /tmp/sre-agent-worker-migration.yaml`

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest ../contracts/compatibility-tests/test_gke_manifests.py -v`

- [ ] **步驟 5：提交**

```bash
git add -- deploy/k8s/jobs deploy/k8s/README.md contracts/compatibility-tests/test_gke_manifests.py README.md
git diff --cached --check
git commit -m "docs(deploy): define ordered GKE release flow"
```

---

### 任務 9：完整 regression、image 與本機 E2E 驗證

**檔案：** 無；本任務只驗證已提交成果。若任何 gate 失敗，先使用 `superpowers:systematic-debugging` 找到根因，再回到擁有該檔案的任務修正並建立獨立提交。

- [ ] **步驟 1：確認工具版本**

Run: `python3 --version`

Run: `node --version`

Run: `docker --version`

Run: `kubectl version --client`

預期：Node major version 是 24；Docker daemon 可用；kubectl 支援 Kustomize。

- [ ] **步驟 2：執行 repository gates**

Run: `make test-contracts`

Run: `make test-backend`

Run: `make test-rca-worker`

Run: `make test-frontend`

預期：pytest、Ruff、Pyright、Angular tests 與 production build 全部 PASS。

- [ ] **步驟 3：重建三個正式 images**

Run: `docker build -t sre-agent-backend:gke-final backend`

Run: `docker build -t sre-agent-frontend:gke-final frontend`

Run: `docker build -t sre-agent-rca-worker:gke-final rca-worker`

預期：三個 build 都使用 checked-in lock file 且成功。

- [ ] **步驟 4：重做 manifest 靜態驗證**

Run: `kubectl kustomize deploy/k8s/base > /tmp/sre-agent-gke-final.yaml`

Run: `kubectl apply --dry-run=client -f /tmp/sre-agent-gke-final.yaml`

Run: `rg 'kind: (Secret|Ingress|Gateway)' /tmp/sre-agent-gke-final.yaml`

預期：前兩個命令成功；最後一個命令找不到超出範圍的資源並回傳 exit 1。

- [ ] **步驟 5：執行既有本機 PostgreSQL/Pub/Sub Emulator E2E**

逐項執行根目錄 README「完整本機啟動紀錄」第 2 至第 9 節。Worker terminal 在既有 exports 後額外執行 `export PUBSUB_AUTO_CREATE=true` 與 `export WORKER_ID=local-e2e-worker`；Outbox terminal 改為只設定專用設定要求的 `DATABASE_URL`、`PUBSUB_PROJECT_ID`、`RCA_TOPIC_ID`，並保留 Emulator client 所需的 `PUBSUB_EMULATOR_HOST`。送出 README 既有 curl webhook 後，使用該節的 SQL/Frontend 驗證最新 RCA 為 `PARTIAL`、worker job 為 `SUCCEEDED`、outbox event 為 `PUBLISHED`。不得連線或建立 Google Cloud 資源。

- [ ] **步驟 6：完成前證據檢查**

Run: `git diff --check`

Run: `git status --short`

Run: `git log --oneline -10`

確認只剩使用者原有或明確保留的未提交修改。若要宣告完成，必須使用 `superpowers:verification-before-completion` 並引用本任務實際輸出。
