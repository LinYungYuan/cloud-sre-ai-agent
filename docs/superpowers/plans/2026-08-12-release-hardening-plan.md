# Release Hardening Implementation Plan

> **三套件架構修訂：** 本計畫執行時以 `backend/`、`rca-worker/`、`frontend/` 三個獨立套件為準。Worker 程式、依賴、lock、tests、migration 與 Dockerfile 全部位於 `rca-worker/`，不得新增至 `backend/`；資料表 owner/grants 以 `contracts/database/table-ownership.yaml` 為準。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the independently deployable API, RCA worker, and Angular application observable, operable, secure by default, and measurable against the approved timing objectives.

**Architecture:** Application-owned health endpoints, structured telemetry, redaction, maintenance commands, and containers are added without provisioning any cloud resources. End-to-end tests use PostgreSQL 18 plus fake Pub/Sub/MCP boundaries to measure webhook, visibility, and RCA deadlines deterministically.

**Tech Stack:** Python 3.11+, FastAPI, OpenTelemetry, structlog, PostgreSQL 18, Docker, Angular, pytest, Playwright.

## Global Constraints

- Do not add Terraform, Kubernetes manifests, GKE/Cloud SQL/Pub/Sub provisioning, or an `infrastructure/` directory.
- Backend and Worker use distinct runtime roles and distinct migration roles; runtime roles must not require DDL privileges.
- Backend and Worker use `alembic_version_backend` and `alembic_version_rca_worker` respectively, and migrations run in that order.
- Tokens, authorization headers, cookies, secrets, and unredacted sensitive payloads never appear in logs.
- API, worker, and Angular images build and run independently.
- Webhook acceptance target is two seconds; Incident visibility target is five seconds; queued RCA terminal target is five minutes.
- All data remains permanently stored in PostgreSQL 18.

---

### Task 1: Structured telemetry and mandatory redaction

**Files:**
- Create: `backend/src/sre_agent/observability/logging.py`
- Create: `backend/src/sre_agent/observability/metrics.py`
- Create: `backend/src/sre_agent/observability/tracing.py`
- Create: `rca-worker/src/sre_rca_worker/observability/logging.py`
- Create: `rca-worker/src/sre_rca_worker/observability/metrics.py`
- Create: `rca-worker/src/sre_rca_worker/observability/tracing.py`
- Create: `backend/src/sre_agent/policy/redaction.py`
- Create: `backend/src/sre_agent/api/middleware/request_logging.py`
- Create: `backend/tests/unit/policy/test_redaction.py`
- Create: `backend/tests/integration/observability/test_request_trace.py`
- Create: `rca-worker/tests/unit/policy/test_redaction.py`
- Create: `rca-worker/tests/integration/observability/test_worker_trace.py`

**Interfaces:**
- Produces contract-defined correlation/trace propagation from webhook/API through outbox to the independent Worker, specialist, MCP, and database result; neither package imports the other's telemetry code.

- [ ] **Step 1: Write redaction tests**

Feed nested mappings/lists containing keys matching authorization, token, cookie, password, secret, api_key, session, and configured domain-sensitive keys. Assert values become `[REDACTED]`, original input is unchanged, max depth/size is bounded, and exceptions cannot reintroduce raw values.

- [ ] **Step 2: Write trace propagation tests**

Assert correlation ID is accepted/generated, returned in response, persisted on delivery/job/run, attached to Pub/Sub attributes, restored by worker, and present on MCP spans. Verify captured logs contain no raw bearer token or fixture secret.

- [ ] **Step 3: Implement telemetry**

Configure JSON logs, OpenTelemetry spans, and counters/histograms for webhook latency, Incident visibility latency, queue lag, RCA duration/status, model calls/tokens, MCP latency/timeouts, specialist status, and evidence count. Apply redaction before serialization, not after logging.

- [ ] **Step 4: Verify and commit**

Run: `cd backend && uv run pytest tests/unit/policy/test_redaction.py tests/integration/observability/test_request_trace.py -v`, then `cd ../rca-worker && uv run pytest tests/unit/policy/test_redaction.py tests/integration/observability/test_worker_trace.py -v`
Expected: PASS.

```bash
git add backend/src/sre_agent/observability backend/src/sre_agent/policy/redaction.py backend/src/sre_agent/api/middleware/request_logging.py backend/tests rca-worker/src/sre_rca_worker/observability rca-worker/tests
git commit -m "feat: add redacted end-to-end telemetry"
```

### Task 2: Health, readiness, and dependency behavior

**Files:**
- Create: `backend/src/sre_agent/api/routers/health.py`
- Create: `rca-worker/src/sre_rca_worker/workers/health.py`
- Create: `backend/tests/contract/api/test_health.py`
- Create: `rca-worker/tests/integration/workers/test_worker_readiness.py`

**Interfaces:**
- Produces API `/health/live`, `/health/ready` and worker health command/module without exposing secrets.

- [ ] **Step 1: Write readiness tests**

Liveness succeeds when process loop is responsive. API readiness requires PostgreSQL connection and completed migrations, but not an MCP call. Worker readiness requires PostgreSQL, Pub/Sub client initialization, Skill Registry validation, and MCP configuration presence; transient MCP endpoint outage is reported as degraded telemetry, not startup crash.

- [ ] **Step 2: Run failing tests**

Run: `cd backend && uv run pytest tests/contract/api/test_health.py -v`, then `cd ../rca-worker && uv run pytest tests/integration/workers/test_worker_readiness.py -v`
Expected: FAIL/404.

- [ ] **Step 3: Implement bounded probes**

Each dependency check has a short timeout and returns only stable component/status codes. Public readiness never returns connection strings, endpoints, exception stacks, or token identifiers.

- [ ] **Step 4: Verify and commit**

Run: `cd backend && uv run pytest tests/contract/api/test_health.py -v`, then `cd ../rca-worker && uv run pytest tests/integration/workers/test_worker_readiness.py -v`
Expected: PASS.

```bash
git add backend/src/sre_agent/api/routers/health.py backend/tests/contract/api/test_health.py rca-worker/src/sre_rca_worker/workers/health.py rca-worker/tests/integration/workers/test_worker_readiness.py
git commit -m "feat: add application health and readiness"
```

### Task 3: Partition maintenance and Grafana source bootstrap CLI

**Files:**
- Create: `backend/src/sre_agent/workers/partition_worker.py`
- Create: `backend/src/sre_agent/cli.py`
- Create: `backend/tests/integration/workers/test_partition_worker.py`
- Create: `backend/tests/integration/test_cli.py`

**Interfaces:**
- Produces `python -m sre_agent.cli ensure-partitions --months-ahead 3`.
- Produces `python -m sre_agent.cli register-grafana-source --name NAME --secret-ref REF`.

- [ ] **Step 1: Write CLI/maintenance tests**

Assert partition command creates current plus requested future monthly partitions idempotently, refuses negative/unbounded values, and uses migration/maintenance DB role. Assert source registration creates/updates source metadata and secret reference only, never accepts or stores the actual bearer token.

- [ ] **Step 2: Run failing tests**

Run: `cd backend && uv run pytest tests/integration/workers/test_partition_worker.py tests/integration/test_cli.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement commands**

Use argparse subcommands with explicit bounds (`months-ahead` 1–12). Source registration prints source UUID needed in webhook URL. All changes create audit records with actor `SYSTEM_CLI`; secrets remain in the external secret provider.

- [ ] **Step 4: Verify and commit**

Run: `cd backend && uv run pytest tests/integration/workers/test_partition_worker.py tests/integration/test_cli.py -v`
Expected: PASS.

```bash
git add backend/src/sre_agent/workers/partition_worker.py backend/src/sre_agent/cli.py backend/tests
git commit -m "feat: add database maintenance and source bootstrap"
```

### Task 4: Independent production containers

**Files:**
- Create: `backend/Dockerfile.api`
- Create: `rca-worker/Dockerfile`
- Create: `rca-worker/.dockerignore`
- Create: `backend/.dockerignore`
- Create: `frontend/Dockerfile`
- Create: `frontend/.dockerignore`
- Create: `frontend/docker/entrypoint.sh`
- Create: `frontend/docker/nginx.conf`
- Create: `scripts/smoke/containers.sh`

**Interfaces:**
- Produces separate immutable API, worker, and frontend images.

- [ ] **Step 1: Add container smoke script**

The script builds three images, asserts API image command runs only FastAPI, worker image command runs only worker, and frontend serves static assets plus runtime-generated `/config.json`. It must fail if an image contains `.env`, tests, Git metadata, or infrastructure files.

- [ ] **Step 2: Implement backend multi-stage images**

Build separate Backend and RCA Worker images from their own package roots. Each uses a non-root runtime user, its own locked `uv.lock`, no compiler/dev dependencies in runtime, read-only application source, and an explicit package-local command. Neither image copies the other package's source. Do not run migrations automatically on every API/worker startup.

- [ ] **Step 3: Implement Angular image**

Build in a Node stage and serve static files in a minimal non-root web server stage. Entrypoint creates `config.json` only from allowlisted `API_BASE_URL`, `LOCALE=zh-TW`, `TIME_ZONE=Asia/Taipei` variables; safely JSON-escape values.

- [ ] **Step 4: Run smoke tests and commit**

Run: `./scripts/smoke/containers.sh`
Expected: three builds and smoke checks PASS.

```bash
git add backend/Dockerfile.api backend/.dockerignore rca-worker/Dockerfile rca-worker/.dockerignore frontend/Dockerfile frontend/.dockerignore frontend/docker scripts/smoke
git commit -m "build: add independent application containers"
```

### Task 5: Timing and failure-mode acceptance suite

**Files:**
- Create: `backend/tests/acceptance/test_timing_objectives.py`
- Create: `backend/tests/acceptance/test_failure_recovery.py`
- Create: `scripts/acceptance/run.sh`
- Modify: `Makefile`
- Modify: `README.md`

**Interfaces:**
- Produces `make acceptance` with machine-readable timing output.

- [ ] **Step 1: Build deterministic acceptance harness**

Use PostgreSQL 18, in-process FastAPI, fake Pub/Sub with redelivery, fake clock where appropriate, and deterministic fake MCP/agent runtimes. Record acceptance-to-response, acceptance-to-queryable-Incident, queued-to-terminal-RCA durations.

- [ ] **Step 2: Test failure modes**

Cover duplicate Grafana delivery, database rollback, Pub/Sub publish failure/recovery, worker crash/redelivery, one MCP timeout causing partial report, all MCP failures, REST query visibility after explicit refresh, stale ETag, unclassified waiting/release, and cross-scope access attempts.

- [ ] **Step 3: Enforce objectives**

Webhook must be `<2s`, authenticated REST Incident query visibility `<5s`, and end-to-end RCA completion `<=300s`; deterministic tests additionally use shorter injected deadlines to avoid five-minute CI waits while proving the same deadline logic. Output JSON includes sample count, p50, p95, max, and pass/fail.

- [ ] **Step 4: Run all release gates**

Run: `make check && make acceptance && ./scripts/smoke/containers.sh && test ! -d infrastructure && git diff --check`
Expected: all commands exit 0.

- [ ] **Step 5: Document operator-owned dependencies and commit**

README documents required environment variables, PostgreSQL 18 migration command, source bootstrap, partition maintenance, health endpoints, runtime roles, Pub/Sub topic/subscription contract, Secret Provider contract, MCP endpoints, and that resource provisioning is outside repository scope.

```bash
git add backend/tests/acceptance scripts/acceptance Makefile README.md
git commit -m "test: add platform release acceptance gates"
```
