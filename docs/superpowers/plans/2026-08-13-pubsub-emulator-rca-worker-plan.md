# Pub/Sub Emulator and RCA Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver each committed RCA job through Google Pub/Sub, execute an idempotent read-only evidence-backed RCA within five minutes, and persist a Traditional Chinese COMPLETE/PARTIAL/FAILED report.

**Architecture:** `rca-worker/` is an independent Python package and deployment; it never imports `backend/src`. PostgreSQL remains the source of truth; Backend's transactional outbox publishes a small contract-defined work message to Pub/Sub, and RCA Worker claims the database job using a lease before invoking specialists. Local development and integration tests use Google's official Pub/Sub Emulator through the same Google client-library adapters used in production. ADK and MCP APIs remain behind typed adapters; telemetry is untrusted data and every observed claim references persisted evidence.

**Tech Stack:** Python 3.11+, asyncio, SQLAlchemy async, PostgreSQL 18, Google Cloud Pub/Sub client, official Google Pub/Sub Emulator, Google ADK pinned in `uv.lock`, MCP, Pydantic v2, pytest.

## Global Constraints

- This plan depends on `2026-08-13-grafana-normalization-operator-ui-plan.md` being complete.
- `rca-worker/` has its own `pyproject.toml`, `uv.lock`, tests, Alembic config, Dockerfile, CLI, image, CI/build, and release version.
- `rca-worker/` and `backend/` must not import each other's source; shared shapes live in `contracts/` and are independently validated by both packages.
- Backend, RCA Worker, and both Alembic streams share one application role while retaining distinct Alembic version tables and migration ownership.
- Pub/Sub message contains only schema version and work identifiers; never AlertValues, raw webhook, labels, evidence, prompt, token, or credential.
- Local/CI broker is the official Google Pub/Sub Emulator, not a fake broker; unit tests may use fakes.
- Production Pub/Sub uses Workload Identity/ADC and never a service-account key.
- RCA deadline is 300 seconds from `QUEUED`; lease is 60 seconds; maximum durable attempts is 3.
- Pub/Sub delivery is at-least-once; PostgreSQL job/report state provides idempotency.
- Evidence/report commit occurs before Pub/Sub ack.
- GCP MCP is enabled only with non-blank `resource.label.project_id` and safe normalized scope.
- AWS MCP is enabled only when a safe rule establishes adequate resource scope.
- No safe scope still executes RCA with no MCP tools and produces PARTIAL when evidence is insufficient.
- MCP tools must be explicitly read-only, capability-allowlisted, endpoint-bound, and schema-validated.
- AlertValues, telemetry, logs, traces, and MCP output are untrusted data, never instructions.
- Raw evidence is stored as exact `BYTEA` plus structured `JSONB`, SHA-256, provenance, and safe metadata.
- Every report claim references evidence UUID plus partition timestamp.
- AI narrative is Traditional Chinese; technical evidence remains unchanged.
- No Chat, conversation worker, Router follow-up, SSE, WebSocket, or production infrastructure provisioning.

---

## File Map

- `docker-compose.yml`: local PostgreSQL plus official Pub/Sub Emulator only.
- `contracts/schemas/rca-job-message-v1.json`: shared Pub/Sub message contract.
- `contracts/database/table-ownership.yaml`: unique migration owner contract.
- `backend/src/sre_agent/integrations/pubsub/`: Backend publisher adapter only.
- `backend/src/sre_agent/workers/outbox_worker.py`: publish full RCA identifier message after commit.
- `rca-worker/src/sre_rca_worker/integrations/pubsub/`: subscriber/bootstrap and worker-side message validator.
- `rca-worker/src/sre_rca_worker/agents/`: Skills, specialists, orchestration, and synthesis.
- `rca-worker/src/sre_rca_worker/integrations/mcp/`: endpoint-specific capability adapters.
- `rca-worker/src/sre_rca_worker/application/rca/`: job claim, evidence persistence, report settlement.
- `rca-worker/src/sre_rca_worker/workers/rca_worker.py`: Pub/Sub delivery handler and process entrypoint.
- `rca-worker/migrations/versions/0001_rca_worker_v1.py`: first worker-owned migration after the legacy Backend baseline.
- `rca-worker/tests/`: unit, PostgreSQL 18, Emulator, contract, and evaluation tests.

---

### Task 0: Scaffold the independent RCA Worker package and ownership contract

**Files:**
- Create: `rca-worker/pyproject.toml`
- Create: `rca-worker/uv.lock`
- Create: `rca-worker/alembic.ini`
- Create: `rca-worker/migrations/env.py`
- Create: `rca-worker/src/sre_rca_worker/__init__.py`
- Create: `rca-worker/tests/unit/test_package_boundary.py`
- Create: `rca-worker/Dockerfile`
- Modify: `Makefile`
- Modify: `README.md`
- Modify: `contracts/database/table-ownership.yaml`
- Modify: `contracts/compatibility-tests/test_contracts.py`

**Interfaces:**
- Produces: independent import root `sre_rca_worker` and project commands rooted at `rca-worker/`.
- Produces: Alembic version table `alembic_version_rca_worker`; Backend remains `alembic_version_backend`.
- Produces: machine-readable table migration ownership contract for the shared application role.

- [ ] **Step 1: Write boundary and ownership RED tests**

Assert `rca-worker/pyproject.toml`, lock, Dockerfile, migrations and package are present; scan worker Python AST imports and reject any `sre_agent` import. Parse `table-ownership.yaml` and assert each catalog table has exactly one `migrationOwner`, Backend migrations cannot modify Worker-owned tables, Worker migrations cannot modify core tables, and the manifest declares the shared application role. Assert the root `Makefile` exposes `test-rca-worker` and that `check` depends on it in addition to contracts, Backend, and Frontend gates.

- [ ] **Step 2: Run and confirm RED**

Run: `UV_CACHE_DIR=$PWD/.uv-cache uv run --project backend pytest contracts/compatibility-tests/test_contracts.py -v`

Expected: FAIL because `rca-worker/` and ownership manifest do not exist.

- [ ] **Step 3: Create the independent package**

Set project name `sre-rca-worker`, import root `sre_rca_worker`, Python `>=3.11`, its own dev tools, and no editable/path dependency on `backend`. Configure Alembic `version_table = alembic_version_rca_worker`. Change Backend to `version_table = alembic_version_backend` with an exact transition helper: inside one transaction, if only legacy `alembic_version` exists, rename it to `alembic_version_backend`; if neither exists, create/use the Backend table normally; if both exist, require equal single revision values and then drop only the redundant legacy table, otherwise fail without mutation. Cover all four catalog states in PostgreSQL integration tests. Never rerun initial DDL merely to establish the new version-table name.

Add `make test-rca-worker` to run Worker pytest, Ruff, and Pyright from `rca-worker/`, and make the root `check` target invoke contracts, Backend, Worker, and Frontend without sharing virtual environments. Update README setup and verification commands to match the real targets.

- [ ] **Step 4: Define migration ownership for the shared role**

Declare Backend migration ownership for source/delivery/alert/Incident/outbox/audit tables, and Worker migration ownership for `rca_runs`, `specialist_runs`, `evidence_records`, `rca_hypotheses`, `hypothesis_evidence`, `rca_reports`, `worker_jobs`, and `worker_attempts`. Declare one shared application role for Backend runtime, Worker runtime, Backend Alembic, and Worker Alembic. The manifest documents code-level access intent but does not pretend PostgreSQL can distinguish the two services when they use the same credential.

- [ ] **Step 5: Verify package isolation and commit**

Run: `cd rca-worker && UV_CACHE_DIR=$PWD/.uv-cache uv sync --frozen && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/test_package_boundary.py -v`

Run the compatibility test from Step 2. Expected: PASS.

```bash
git add rca-worker contracts/database/table-ownership.yaml contracts/compatibility-tests/test_contracts.py backend/migrations/env.py Makefile README.md
git commit -m "build: scaffold independent RCA worker package"
```

---

### Task 1: Define and publish the RCA work message

**Files:**
- Create: `contracts/schemas/rca-job-message-v1.json`
- Create: `backend/src/sre_agent/integrations/pubsub/messages.py`
- Create: `rca-worker/src/sre_rca_worker/integrations/pubsub/messages.py`
- Modify: `backend/src/sre_agent/workers/outbox_worker.py`
- Modify: `backend/src/sre_agent/persistence/repositories/jobs.py`
- Modify: `backend/tests/integration/workers/test_outbox_worker.py`
- Test: `backend/tests/unit/integrations/pubsub/test_messages.py`
- Test: `rca-worker/tests/contract/test_rca_job_message.py`

**Interfaces:**
- Produces: `RcaJobMessage(schema_version: Literal[1], worker_job_id: UUID, rca_run_id: UUID, incident_id: UUID, attempt: Literal[1])`.
- Produces: equivalent local `RcaJobMessage` adapters in both packages, each validated against `contracts/schemas/rca-job-message-v1.json`; neither package imports the other's model.

- [ ] **Step 1: Write strict message RED tests**

```python
def test_rca_job_message_is_canonical_and_minimal():
    encoded = message.to_bytes()
    assert json.loads(encoded) == {
        "schemaVersion": 1,
        "workerJobId": str(worker_job_id),
        "rcaRunId": str(rca_run_id),
        "incidentId": str(incident_id),
        "attempt": 1,
    }
    assert b"AlertValues" not in encoded
```

Reject extra keys, unknown version, mismatched UUID types, attempt other than 1, and payloads over a small fixed ceiling such as 1 KiB.
Run the same checked-in valid/invalid examples through both package adapters and assert canonical bytes are identical.

- [ ] **Step 2: Run and confirm RED**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/integrations/pubsub/test_messages.py -v`

Run: `cd rca-worker && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/contract/test_rca_job_message.py -v`

Expected: collection FAIL for missing module.

- [ ] **Step 3: Implement the Pydantic message**

Define the JSON Schema first. Implement the same `ConfigDict(extra="forbid", frozen=True, populate_by_name=True)` contract separately in Backend and Worker, with canonical `json.dumps(self.model_dump(by_alias=True, mode="json"), sort_keys=True, separators=(",", ":"))`. Compatibility tests compare both adapters to the shared schema/examples.

- [ ] **Step 4: Make `create_rca_work` put all identifiers in outbox payload**

Insert the worker job first with `RETURNING id`, then write payload:

```json
{"schemaVersion":1,"workerJobId":"10000000-0000-0000-0000-000000000001","rcaRunId":"20000000-0000-0000-0000-000000000002","incidentId":"30000000-0000-0000-0000-000000000003","attempt":1}
```

Keep `idempotency_key = rca-run:{rca_run_id}` and enforce one job per run/type.

- [ ] **Step 5: Publish stored payload, not a reconstructed generic envelope**

Change `OutboxPublisher` query to select `payload`; validate it with `RcaJobMessage.from_mapping` for `RCA_RUN_REQUESTED`, publish `message.to_bytes()`, and retain `idempotencyKey` attribute. A schema-invalid stored payload raises `OutboxInvariantError`, rolls back the batch, and terminates the publisher run visibly for operator remediation; it must not be classified as a transient Pub/Sub failure or have its `available_at` silently advanced.

- [ ] **Step 6: Run unit and PostgreSQL integration tests**

Run: `cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/integrations/pubsub/test_messages.py tests/integration/workers/test_outbox_worker.py -v`

Run the Worker contract test from Step 2.

Expected: PASS, including cancellation settlement tests.

- [ ] **Step 7: Commit**

```bash
git add contracts/schemas/rca-job-message-v1.json backend/src/sre_agent/integrations/pubsub/messages.py backend/src/sre_agent/workers/outbox_worker.py backend/src/sre_agent/persistence/repositories/jobs.py backend/tests rca-worker/src/sre_rca_worker/integrations/pubsub/messages.py rca-worker/tests/contract/test_rca_job_message.py
git commit -m "feat: publish typed RCA work messages"
```

---

### Task 2: Add the official Pub/Sub Emulator and bootstrap

**Files:**
- Modify: `docker-compose.yml`
- Create: `rca-worker/src/sre_rca_worker/integrations/pubsub/bootstrap.py`
- Create: `rca-worker/src/sre_rca_worker/integrations/pubsub/subscriber.py`
- Create: `rca-worker/src/sre_rca_worker/config/settings.py`
- Create: `backend/src/sre_agent/workers/outbox_main.py`
- Modify: `backend/pyproject.toml`
- Create: `rca-worker/tests/integration/pubsub/conftest.py`
- Create: `rca-worker/tests/integration/pubsub/test_emulator_delivery.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `ensure_topic_and_subscription(project_id: str, topic_id: str, subscription_id: str) -> None`.
- Produces: `PubSubDelivery(data: bytes, ack: Callable[[], None], nack: Callable[[], None])` adapter.
- Produces settings `pubsub_subscription_id`, optional `pubsub_emulator_host` allowed only outside production.
- Produces worker CLI `sre-agent-outbox-worker` using Emulator when configured and ADC otherwise.

- [ ] **Step 1: Write Compose and delivery RED tests**

Add a semantic YAML assertion that service `pubsub-emulator` uses the official Google image, exact version, loopback binding, and no credential:

```python
assert service["image"] == "google/cloud-sdk:578.0.0-emulators"
assert service["platform"] == "linux/amd64"
assert service["ports"] == ["127.0.0.1:58085:8085"]
```

The integration test must create an isolated topic/subscription, publish through `GooglePubSubPublisher`, pull through the subscriber, nack once, observe redelivery, then ack.

- [ ] **Step 2: Run RED before adding the service**

Run: `UV_CACHE_DIR=$PWD/.uv-cache uv run --project backend pytest contracts/compatibility-tests/test_contracts.py -v`

Run: `cd rca-worker && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/integration/pubsub/test_emulator_delivery.py -v`

Expected: FAIL because Compose service/bootstrap/subscriber are absent.

- [ ] **Step 3: Add the official Emulator service**

Add only local development support:

```yaml
pubsub-emulator:
  image: google/cloud-sdk:578.0.0-emulators
  platform: linux/amd64
  command: ["gcloud", "beta", "emulators", "pubsub", "start", "--project=sre-agent-local", "--host-port=0.0.0.0:8085"]
  ports: ["127.0.0.1:58085:8085"]
  healthcheck:
    test: ["CMD-SHELL", "python3 -c 'import socket; socket.create_connection((\"127.0.0.1\",8085),2)'" ]
```

Do not create an `infrastructure/` directory or production resources.

- [ ] **Step 4: Implement idempotent client-library bootstrap**

Use `PublisherClient`/`SubscriberClient`; accept AlreadyExists, propagate other errors. When `PUBSUB_EMULATOR_HOST` is set, client libraries use the Emulator and no credentials. In production, reject an emulator host and rely on ADC.

Implement `outbox_main.py` as the production composition root: build async engine/session factory, construct the Google publisher and `OutboxPublisher`, publish bounded batches until shutdown, and close the Pub/Sub client/engine in `finally`. Add `sre-agent-outbox-worker = "sre_agent.workers.outbox_main:main"`; safe startup failures return exit code 1 without printing settings or secrets.

- [ ] **Step 5: Start Emulator and run real integration tests**

Run: `docker compose up -d pubsub-emulator`

Run: `cd rca-worker && PUBSUB_EMULATOR_HOST=127.0.0.1:58085 PUBSUB_PROJECT_ID=sre-agent-test UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/integration/pubsub/test_emulator_delivery.py -v`

Expected: PASS with observed redelivery and ack.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml rca-worker/src/sre_rca_worker/integrations/pubsub rca-worker/src/sre_rca_worker/config rca-worker/tests/integration/pubsub backend/src/sre_agent/workers/outbox_main.py backend/pyproject.toml README.md contracts/compatibility-tests/test_contracts.py
git commit -m "feat: run RCA delivery on Pub/Sub Emulator"
```

---

### Task 3: Add RCA worker schema, leases, attempts, and exact evidence

**Files:**
- Create: `rca-worker/migrations/versions/0001_rca_worker_v1.py`
- Create: `rca-worker/tests/integration/persistence/test_schema.py`
- Create: `rca-worker/tests/unit/persistence/test_schema_documentation.py`
- Modify: `docs/database/postgresql-schema.md`
- Modify: `contracts/database/table-ownership.yaml`

**Interfaces:**
- Produces: worker lease fields, exact evidence bytes/metadata, safe failure codes, and report status constraints.
- Consumes: Backend schema revision `0002_grafana_normalization_v2` as an external prerequisite, not an Alembic `down_revision` in the Worker version chain.
- Produces: Worker revision `0001_rca_worker_v1` recorded in `alembic_version_rca_worker`.

- [ ] **Step 1: Write catalog RED tests**

Assert worker jobs include `lease_owner`, `lease_expires_at`, and `attempt_count CHECK 0..3`; RCA runs, specialist runs, and worker attempts store an allowlisted `failure_code` instead of sensitive exception text. Assert evidence has `raw_result BYTEA`, `structured_data JSONB`, `metadata JSONB`, `content_hash`, and partitioned PK. Assert report result status is `COMPLETE|PARTIAL|FAILED`.

- [ ] **Step 2: Run PostgreSQL 18 RED**

Run Backend migration first, then Worker RED:

`cd backend && POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic upgrade head`

`cd rca-worker && POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic upgrade head && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run pytest tests/integration/persistence/test_schema.py -v`

Expected: FAIL on missing worker/evidence columns.

- [ ] **Step 3: Implement revision 0003**

At migration start, query `alembic_version_backend` and fail safely unless the required Backend head/revision is present. This first Worker revision adopts the already-existing legacy RCA/worker tables: it validates their exact baseline columns/constraints and alters them in place; it must not recreate or drop them. Use named CHECK/FK/indexes only on Worker-owned tables. Replace `raw_result_reference` only after backfilling legacy rows with empty bytes/metadata and an explicit legacy marker. Add allowlisted `failure_code` columns and remove legacy `error_message` columns from `rca_runs`, `specialist_runs`, and `worker_attempts`; downgrade recreates them as nullable without attempting to reconstruct discarded sensitive text. Add `result_status` to `rca_reports` with `COMPLETE|PARTIAL|FAILED`. Add an index on `(status, available_at, lease_expires_at)`. Worker downgrade to base reverses only Worker-added alterations and leaves the legacy tables/data owned by the installed baseline intact. Do not modify core Incident/Alert/outbox DDL.

- [ ] **Step 4: Verify downgrade/upgrade and documentation parity**

Run Worker downgrade to `base`, Worker upgrade to `head`, schema tests, ownership contract tests, and documentation parser tests while leaving Backend at its head. Expected: PASS. Document downgrade evidence-data loss and the required Backend → Worker migration order.

- [ ] **Step 5: Commit**

```bash
git add rca-worker/migrations/versions/0001_rca_worker_v1.py rca-worker/tests/integration/persistence/test_schema.py rca-worker/tests/unit/persistence/test_schema_documentation.py docs/database/postgresql-schema.md contracts/database/table-ownership.yaml
git commit -m "feat: add durable RCA worker schema"
```

---

### Task 4: Add strict Skill registry and untrusted-data boundary

**Files:**
- Create: `rca-worker/src/sre_rca_worker/agents/skills/models.py`
- Create: `rca-worker/src/sre_rca_worker/agents/skills/loader.py`
- Create: `rca-worker/src/sre_rca_worker/agents/skills/registry.py`
- Create: `rca-worker/src/sre_rca_worker/agents/skills/definitions/metrics-analysis/SKILL.md`
- Create: `rca-worker/src/sre_rca_worker/agents/skills/definitions/trace-analysis/SKILL.md`
- Create: `rca-worker/src/sre_rca_worker/agents/skills/definitions/log-analysis/SKILL.md`
- Create: `rca-worker/src/sre_rca_worker/agents/skills/definitions/rca-analysis/SKILL.md`
- Test: `rca-worker/tests/unit/agents/skills/test_registry.py`

**Interfaces:**
- Produces: `SkillSpec(name, agent, description, required_capabilities, risk, body)`.
- Produces: `SkillRegistry.get_for_agent(agent_name: str) -> SkillSpec`.

- [ ] **Step 1: Write registry RED tests**

Assert four unique skills, non-empty required capabilities for specialists, `risk == READ_ONLY`, strict YAML frontmatter, no tool names/endpoints in definitions, zh-TW report rule, evidence citation rule, and explicit text that AlertValues/telemetry/tool output are data rather than instructions.

- [ ] **Step 2: Run RED**

Run: `cd rca-worker && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/agents/skills/test_registry.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement strict loader and definitions**

Parse frontmatter with Pydantic `extra="forbid"`; reject duplicate names, path traversal, missing sections, mutation risk, and direct tool names. Define canonical capabilities only.

- [ ] **Step 4: Run tests and commit**

Run Task 4 tests. Expected: PASS.

```bash
git add rca-worker/src/sre_rca_worker/agents/skills rca-worker/tests/unit/agents/skills
git commit -m "feat: add read-only RCA skill registry"
```

---

### Task 5: Pin ADK and isolate MCP capabilities by endpoint

**Files:**
- Modify: `rca-worker/pyproject.toml`
- Modify: `rca-worker/uv.lock`
- Create: `rca-worker/src/sre_rca_worker/integrations/mcp/models.py`
- Create: `rca-worker/src/sre_rca_worker/integrations/mcp/client.py`
- Create: `rca-worker/src/sre_rca_worker/integrations/mcp/capability_resolver.py`
- Create: `rca-worker/src/sre_rca_worker/integrations/mcp/factories.py`
- Test: `rca-worker/tests/unit/integrations/mcp/test_capability_resolver.py`
- Test: `rca-worker/tests/contract/mcp/test_endpoint_isolation.py`

**Interfaces:**
- Produces: `McpClient.list_tools()`, `McpClient.call(tool_name, arguments, deadline)`.
- Produces: `CapabilityResolver.resolve(required, manifest, discovered) -> tuple[AllowedTool, ...]`.
- Produces: `McpClientFactory.for_specialist(kind, scope) -> McpClient` with no arbitrary endpoint argument.

- [ ] **Step 1: Write allowlist/isolation RED tests**

Test missing or ambiguous capability fails closed; mutation/unknown annotations reject; endpoint identity must match trusted manifest; Metrics cannot call Log/Trace tools; invalid input schema rejects before network; no scope yields an empty client without connecting.

- [ ] **Step 2: Run RED**

Run: `cd rca-worker && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/integrations/mcp tests/contract/mcp -v`

Expected: collection FAIL.

- [ ] **Step 3: Add official Google ADK and MCP dependencies with an exact lock**

Run `cd rca-worker && UV_CACHE_DIR=$PWD/.uv-cache uv add google-adk mcp`, commit the Worker-owned `uv.lock`, and never use a floating runtime install. Backend must not gain ADK/MCP dependencies. Wrap all SDK imports inside Worker adapter modules.

- [ ] **Step 4: Implement trusted capability manifest and factories**

Manifest entries contain endpoint identity, capabilities, allowed tool-name pattern, input schema hash, and `READ_ONLY`. Production auth uses ADC/Workload Identity; local Bearer is read from injected `SecretProvider` and never stored in `AllowedTool` repr/log.

- [ ] **Step 5: Run tests/static checks and commit**

Run Task 5 tests, Ruff, and Pyright. Expected: PASS/0 errors.

```bash
git add rca-worker/pyproject.toml rca-worker/uv.lock rca-worker/src/sre_rca_worker/integrations/mcp rca-worker/tests/unit/integrations/mcp rca-worker/tests/contract/mcp
git commit -m "feat: isolate RCA MCP capabilities"
```

---

### Task 6: Define specialist and evidence contracts

**Files:**
- Create: `rca-worker/src/sre_rca_worker/domain/evidence/models.py`
- Create: `rca-worker/src/sre_rca_worker/agents/specialists/base.py`
- Create: `rca-worker/src/sre_rca_worker/agents/specialists/metrics_agent.py`
- Create: `rca-worker/src/sre_rca_worker/agents/specialists/trace_agent.py`
- Create: `rca-worker/src/sre_rca_worker/agents/specialists/log_agent.py`
- Test: `rca-worker/tests/unit/agents/specialists/test_contracts.py`

**Interfaces:**
- Produces: `EvidenceDraft`, `EvidenceReference(id: UUID, partition_timestamp: datetime)`, `SpecialistRequest`, `SpecialistResult`.
- Produces: `Specialist.run(request, deadline) -> SpecialistResult`.

- [ ] **Step 1: Write Pydantic contract RED tests**

Reject confidence outside `[0,1]`, naive time, window outside Incident request, endpoint/tool mismatch, raw result not bytes/JSON, claims without evidence, and scope differing from normalized safe scope. Assert `AlertIssue` remains a separate untrusted data field.

- [ ] **Step 2: Run RED**

Run: `cd rca-worker && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/agents/specialists/test_contracts.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement immutable contracts and adapters**

Specialists return domain-limited findings only. `EvidenceDraft` contains endpoint identity, capability, tool, input scope, observed/time window, structured JSON, exact raw bytes, content type, and SHA-256 input. No specialist may declare final root cause.

- [ ] **Step 4: Add no-safe-scope behavior**

When `available_tools == ()`, specialist adapters do not instantiate/call MCP and return a deterministic missing-evidence result. Test both blank GCP project and unclassified AWS resource.

- [ ] **Step 5: Run tests and commit**

```bash
git add rca-worker/src/sre_rca_worker/domain/evidence rca-worker/src/sre_rca_worker/agents/specialists rca-worker/tests/unit/agents/specialists
git commit -m "feat: define RCA specialist evidence contracts"
```

---

### Task 7: Orchestrate specialists within the five-minute deadline

**Files:**
- Create: `rca-worker/src/sre_rca_worker/agents/rca/models.py`
- Create: `rca-worker/src/sre_rca_worker/agents/rca/workflow.py`
- Test: `rca-worker/tests/unit/agents/rca/test_workflow.py`

**Interfaces:**
- Produces: `RcaWorkflow.run(context: IncidentContext, deadline: datetime) -> InvestigationBundle`.
- Consumes: Task 6 specialists.

- [ ] **Step 1: Write concurrency/deadline RED tests**

Use controllable fake specialists. Assert all eligible specialists start before any completes; no-safe-scope starts none; one timeout preserves other results; global cancellation stops new calls; deadline-before-start returns no invocation; result ordering is deterministic.

- [ ] **Step 2: Run RED**

Run: `cd rca-worker && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/agents/rca/test_workflow.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement structured concurrency**

Use `asyncio.TaskGroup`, a global aware-UTC deadline, per-call remaining-time calculation, bounded retry only for transient transport errors, and explicit `SpecialistFailure` values. Re-raise caller cancellation.

- [ ] **Step 4: Run tests and commit**

```bash
git add rca-worker/src/sre_rca_worker/agents/rca rca-worker/tests/unit/agents/rca/test_workflow.py
git commit -m "feat: run RCA specialists within deadline"
```

---

### Task 8: Persist evidence and synthesize cited Traditional Chinese reports

**Files:**
- Create: `rca-worker/src/sre_rca_worker/application/rca/persist_evidence.py`
- Create: `rca-worker/src/sre_rca_worker/persistence/repositories/rca.py`
- Create: `rca-worker/src/sre_rca_worker/domain/rca/models.py`
- Create: `rca-worker/src/sre_rca_worker/agents/rca/synthesizer.py`
- Test: `rca-worker/tests/integration/application/test_persist_evidence.py`
- Test: `rca-worker/tests/unit/agents/rca/test_synthesizer.py`

**Interfaces:**
- Produces: persisted `EvidenceReference(UUID, partition_timestamp)` before synthesis.
- Produces: `RcaReportDraft(status, summary_zh_tw, claims, hypotheses, missing_evidence, verification_steps)`.

- [ ] **Step 1: Write exact evidence RED tests**

Persist bytes containing whitespace/order/duplicate JSON keys and prove exact BYTEA/hash round-trip. Assert JSONB is separately parsed, metadata has endpoint/capability/tool/scope/window, and reports never copy raw evidence.

- [ ] **Step 2: Write hallucination/citation RED tests**

Reject unknown evidence UUID or wrong partition timestamp; reject observed facts without SUPPORTS/CONTRADICTS/MISSING relation; reject COMPLETE when required evidence is absent. No MCP scope must produce PARTIAL with `證據不足` and no invented provider/resource/root cause.

- [ ] **Step 3: Run RED**

Run: `cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/integration/application/test_persist_evidence.py tests/unit/agents/rca/test_synthesizer.py -v`

Expected: collection FAIL.

- [ ] **Step 4: Implement content-addressed persistence and validated synthesis**

Persist evidence first, pass only safe summaries plus opaque references to synthesis, validate the structured result, and allow one corrective retry for schema/citation failures while deadline remains. Status rules: sufficient evidence -> COMPLETE; partial/no safe scope -> PARTIAL; no usable analysis or permanent workflow failure -> FAILED.

- [ ] **Step 5: Run tests and commit**

```bash
git add rca-worker/src/sre_rca_worker/application/rca rca-worker/src/sre_rca_worker/persistence/repositories/rca.py rca-worker/src/sre_rca_worker/domain/rca rca-worker/src/sre_rca_worker/agents/rca/synthesizer.py rca-worker/tests
git commit -m "feat: persist evidence-backed RCA reports"
```

---

### Task 9: Implement the idempotent RCA worker and Pub/Sub settlement

**Files:**
- Create: `rca-worker/src/sre_rca_worker/application/rca/job_lifecycle.py`
- Create: `rca-worker/src/sre_rca_worker/workers/rca_worker.py`
- Modify: `rca-worker/pyproject.toml`
- Create: `rca-worker/tests/integration/workers/test_rca_worker.py`
- Create: `rca-worker/tests/integration/pubsub/test_rca_worker_emulator.py`

**Interfaces:**
- Produces: `RcaJobHandler.handle(message: RcaJobMessage) -> JobDisposition`.
- Produces: worker CLI `sre-agent-rca-worker`.

- [ ] **Step 1: Write lifecycle/concurrency RED tests**

Cover valid success, no-scope PARTIAL/no MCP, specialist partial failure, complete failure, duplicate delivery, concurrent consumers, crash after evidence commit, stale 60-second lease recovery, three attempts maximum, mismatched message identifiers, report already terminal, and DB settlement failure.

- [ ] **Step 2: Run RED**

Run: `cd rca-worker && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/integration/workers/test_rca_worker.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement atomic claim and lease renewal**

Claim only QUEUED or expired RUNNING rows with `FOR UPDATE SKIP LOCKED`; atomically set owner, `lease_expires_at = now + 60 seconds`, increment attempt, and set `deadline_at = queued_at + 300 seconds`. Lease renewal must compare owner and non-terminal state; lost lease cancels further MCP calls and prevents settlement.

- [ ] **Step 4: Implement durable stage settlement**

Persist attempts, evidence, report, timeline/status, job/run terminal state in recoverable transactions. Ack only after terminal commit. Nack transient failures only if attempt < 3 and deadline remains. Permanently invalid/policy/auth messages record a safe failure code and ack. Never store raw exception text.

- [ ] **Step 5: Test through the official Emulator**

Start PostgreSQL and Emulator, publish a real outbox message, run one delivery, and assert the Pub/Sub message is acked only after report/job commit. Force one nack/redelivery and assert one final report.

Run: `cd rca-worker && PUBSUB_EMULATOR_HOST=127.0.0.1:58085 PUBSUB_PROJECT_ID=sre-agent-test MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/integration/pubsub/test_rca_worker_emulator.py -v`

Expected: PASS.

- [ ] **Step 6: Add the CLI and commit**

Add `sre-agent-rca-worker = "sre_rca_worker.workers.rca_worker:main"` to the Worker project. `main` must return 0/1, close subscriber/DB/MCP resources in `finally`, and never print secrets.

```bash
git add rca-worker/src/sre_rca_worker/application/rca rca-worker/src/sre_rca_worker/workers/rca_worker.py rca-worker/pyproject.toml rca-worker/tests
git commit -m "feat: execute durable RCA jobs"
```

---

### Task 10: Add evaluation datasets and complete release verification

**Files:**
- Create: `rca-worker/tests/eval/datasets/gcp-safe-scope.json`
- Create: `rca-worker/tests/eval/datasets/aws-safe-scope.json`
- Create: `rca-worker/tests/eval/datasets/no-safe-scope.json`
- Create: `rca-worker/tests/eval/test_rca_reports.py`
- Modify: `README.md`
- Modify: `docs/database/postgresql-schema.md`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: deterministic quality/safety gates and local Emulator runbook.

- [ ] **Step 1: Write evaluation RED tests**

Each dataset contains fixed untrusted AlertValues, fixed MCP evidence (or none), expected status, required/forbidden claims, required citations, and zh-TW phrases. Include prompt injection and URL instructions that must not trigger tools.

- [ ] **Step 2: Run RED and implement deterministic fake runtime wiring**

Run: `cd rca-worker && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/eval/test_rca_reports.py -v`

Expected initially: FAIL on missing eval composition. Add an injected deterministic runtime only in tests; production remains ADK-backed.

- [ ] **Step 3: Run fresh PostgreSQL and Emulator acceptance**

```bash
docker compose up -d postgres pubsub-emulator
cd backend
POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic downgrade base
POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic upgrade head
cd ../rca-worker
POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic upgrade head
PUBSUB_EMULATOR_HOST=127.0.0.1:58085 PUBSUB_PROJECT_ID=sre-agent-test MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests -v
```

Expected: all backend tests PASS.

- [ ] **Step 4: Run contract/static/final safety gates**

```bash
UV_CACHE_DIR=$PWD/.uv-cache uv run --project backend pytest contracts/compatibility-tests -v
cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run ruff check . && UV_CACHE_DIR=$PWD/.uv-cache uv run pyright
cd ../rca-worker && UV_CACHE_DIR=$PWD/.uv-cache uv run ruff check . && UV_CACHE_DIR=$PWD/.uv-cache uv run pyright
git diff --check
test ! -d infrastructure
```

Also scan tracked files for service-account private keys, Authorization values, `POSTGRES_PASSWORD`, and raw AlertValues in Pub/Sub fixtures. Expected: no findings.

- [ ] **Step 5: Update runbook and commit**

Document `docker compose up -d postgres pubsub-emulator`, `PUBSUB_EMULATOR_HOST`, bootstrap, outbox worker, RCA worker, official Emulator limitations, production ADC requirement, 300/60/3 lifecycle, and no Chat scope.

```bash
git add rca-worker/tests/eval README.md docs/database/postgresql-schema.md contracts/database/table-ownership.yaml
git commit -m "test: verify evidence-backed RCA workflow"
```
