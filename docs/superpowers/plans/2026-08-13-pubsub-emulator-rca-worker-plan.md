# Pub/Sub Emulator and RCA Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver each committed RCA job through Google Pub/Sub, execute an idempotent read-only evidence-backed RCA within five minutes, and persist a Traditional Chinese COMPLETE/PARTIAL/FAILED report.

**Architecture:** PostgreSQL remains the source of truth; the transactional outbox publishes a small typed work message to Pub/Sub, and a separate RCA worker claims the database job using a lease before invoking specialists. Local development and integration tests use Google's official Pub/Sub Emulator through the same Google client-library adapters used in production. ADK and MCP APIs remain behind typed adapters; telemetry is untrusted data and every observed claim references persisted evidence.

**Tech Stack:** Python 3.11+, asyncio, SQLAlchemy async, PostgreSQL 18, Google Cloud Pub/Sub client, official Google Pub/Sub Emulator, Google ADK pinned in `uv.lock`, MCP, Pydantic v2, pytest.

## Global Constraints

- This plan depends on `2026-08-13-grafana-normalization-operator-ui-plan.md` being complete.
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
- `backend/src/sre_agent/integrations/pubsub/`: typed publisher/subscriber/bootstrap and message contract.
- `backend/src/sre_agent/workers/outbox_worker.py`: publish full RCA identifier message after commit.
- `backend/src/sre_agent/agents/skills/`: strict read-only skill definitions.
- `backend/src/sre_agent/integrations/mcp/`: endpoint-specific capability adapters.
- `backend/src/sre_agent/agents/specialists/`: Metrics/Trace/Logs evidence producers.
- `backend/src/sre_agent/agents/rca/`: deadline orchestration and synthesis.
- `backend/src/sre_agent/application/rca/`: job claim, evidence persistence, report settlement.
- `backend/src/sre_agent/workers/rca_worker.py`: Pub/Sub delivery handler and process entrypoint.
- `backend/migrations/versions/0003_rca_worker_v1.py`: lease, attempt, evidence, and report constraints.
- `backend/tests/integration/pubsub/`: real Emulator integration tests.
- `backend/tests/eval/datasets/`: deterministic evidence/report evaluation cases.

---

### Task 1: Define and publish the RCA work message

**Files:**
- Create: `backend/src/sre_agent/integrations/pubsub/messages.py`
- Modify: `backend/src/sre_agent/workers/outbox_worker.py`
- Modify: `backend/src/sre_agent/persistence/repositories/jobs.py`
- Modify: `backend/tests/integration/workers/test_outbox_worker.py`
- Test: `backend/tests/unit/integrations/pubsub/test_messages.py`

**Interfaces:**
- Produces: `RcaJobMessage(schema_version: Literal[1], worker_job_id: UUID, rca_run_id: UUID, incident_id: UUID, attempt: Literal[1])`.
- Produces: `RcaJobMessage.to_bytes() -> bytes`, `RcaJobMessage.from_bytes(data: bytes) -> RcaJobMessage`, and `RcaJobMessage.from_mapping(value: Mapping[str, object]) -> RcaJobMessage`.

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

- [ ] **Step 2: Run and confirm RED**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/integrations/pubsub/test_messages.py -v`

Expected: collection FAIL for missing module.

- [ ] **Step 3: Implement the Pydantic message**

Use `ConfigDict(extra="forbid", frozen=True, populate_by_name=True)` and canonical `json.dumps(self.model_dump(by_alias=True, mode="json"), sort_keys=True, separators=(",", ":"))`.

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

Expected: PASS, including cancellation settlement tests.

- [ ] **Step 7: Commit**

```bash
git add backend/src/sre_agent/integrations/pubsub/messages.py backend/src/sre_agent/workers/outbox_worker.py backend/src/sre_agent/persistence/repositories/jobs.py backend/tests
git commit -m "feat: publish typed RCA work messages"
```

---

### Task 2: Add the official Pub/Sub Emulator and bootstrap

**Files:**
- Modify: `docker-compose.yml`
- Create: `backend/src/sre_agent/integrations/pubsub/bootstrap.py`
- Create: `backend/src/sre_agent/integrations/pubsub/subscriber.py`
- Modify: `backend/src/sre_agent/config/settings.py`
- Create: `backend/src/sre_agent/workers/outbox_main.py`
- Modify: `backend/pyproject.toml`
- Create: `backend/tests/integration/pubsub/conftest.py`
- Create: `backend/tests/integration/pubsub/test_emulator_delivery.py`
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

Run: `UV_CACHE_DIR=$PWD/.uv-cache uv run --project backend pytest contracts/compatibility-tests/test_contracts.py backend/tests/integration/pubsub/test_emulator_delivery.py -v`

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

Run: `cd backend && PUBSUB_EMULATOR_HOST=127.0.0.1:58085 PUBSUB_PROJECT_ID=sre-agent-test UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/integration/pubsub/test_emulator_delivery.py -v`

Expected: PASS with observed redelivery and ack.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml backend/src/sre_agent/integrations/pubsub backend/src/sre_agent/config/settings.py backend/src/sre_agent/workers/outbox_main.py backend/pyproject.toml backend/tests/integration/pubsub README.md contracts/compatibility-tests/test_contracts.py
git commit -m "feat: run RCA delivery on Pub/Sub Emulator"
```

---

### Task 3: Add RCA worker schema, leases, attempts, and exact evidence

**Files:**
- Create: `backend/migrations/versions/0003_rca_worker_v1.py`
- Modify: `backend/tests/integration/persistence/test_schema.py`
- Modify: `backend/tests/unit/persistence/test_schema_documentation.py`
- Modify: `docs/database/postgresql-schema.md`

**Interfaces:**
- Produces: worker lease fields, exact evidence bytes/metadata, safe failure codes, and report status constraints.
- Consumes: migration `0002_grafana_normalization_v2`.

- [ ] **Step 1: Write catalog RED tests**

Assert worker jobs include `lease_owner`, `lease_expires_at`, and `attempt_count CHECK 0..3`; RCA runs, specialist runs, and worker attempts store an allowlisted `failure_code` instead of sensitive exception text. Assert evidence has `raw_result BYTEA`, `structured_data JSONB`, `metadata JSONB`, `content_hash`, and partitioned PK. Assert report result status is `COMPLETE|PARTIAL|FAILED`.

- [ ] **Step 2: Run PostgreSQL 18 RED**

Run: `cd backend && POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic upgrade head && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run pytest tests/integration/persistence/test_schema.py -v`

Expected: FAIL on missing worker/evidence columns.

- [ ] **Step 3: Implement revision 0003**

Use named CHECK/FK/indexes. Replace `raw_result_reference` only after backfilling legacy rows with empty bytes/metadata and an explicit legacy marker. Add allowlisted `failure_code` columns and remove legacy `error_message` columns from `rca_runs`, `specialist_runs`, and `worker_attempts`; downgrade recreates them as nullable without attempting to reconstruct discarded sensitive text. Add `result_status` to `rca_reports` with `COMPLETE|PARTIAL|FAILED`. Add an index on `(status, available_at, lease_expires_at)`.

- [ ] **Step 4: Verify downgrade/upgrade and documentation parity**

Run downgrade to `0002_grafana_normalization_v2`, upgrade to head, schema tests, and documentation parser tests. Expected: PASS. Document downgrade evidence-data loss.

- [ ] **Step 5: Commit**

```bash
git add backend/migrations/versions/0003_rca_worker_v1.py backend/tests/integration/persistence/test_schema.py backend/tests/unit/persistence/test_schema_documentation.py docs/database/postgresql-schema.md
git commit -m "feat: add durable RCA worker schema"
```

---

### Task 4: Add strict Skill registry and untrusted-data boundary

**Files:**
- Create: `backend/src/sre_agent/agents/skills/models.py`
- Create: `backend/src/sre_agent/agents/skills/loader.py`
- Create: `backend/src/sre_agent/agents/skills/registry.py`
- Create: `backend/src/sre_agent/agents/skills/definitions/metrics-analysis/SKILL.md`
- Create: `backend/src/sre_agent/agents/skills/definitions/trace-analysis/SKILL.md`
- Create: `backend/src/sre_agent/agents/skills/definitions/log-analysis/SKILL.md`
- Create: `backend/src/sre_agent/agents/skills/definitions/rca-analysis/SKILL.md`
- Test: `backend/tests/unit/agents/skills/test_registry.py`

**Interfaces:**
- Produces: `SkillSpec(name, agent, description, required_capabilities, risk, body)`.
- Produces: `SkillRegistry.get_for_agent(agent_name: str) -> SkillSpec`.

- [ ] **Step 1: Write registry RED tests**

Assert four unique skills, non-empty required capabilities for specialists, `risk == READ_ONLY`, strict YAML frontmatter, no tool names/endpoints in definitions, zh-TW report rule, evidence citation rule, and explicit text that AlertValues/telemetry/tool output are data rather than instructions.

- [ ] **Step 2: Run RED**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/agents/skills/test_registry.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement strict loader and definitions**

Parse frontmatter with Pydantic `extra="forbid"`; reject duplicate names, path traversal, missing sections, mutation risk, and direct tool names. Define canonical capabilities only.

- [ ] **Step 4: Run tests and commit**

Run Task 4 tests. Expected: PASS.

```bash
git add backend/src/sre_agent/agents/skills backend/tests/unit/agents/skills
git commit -m "feat: add read-only RCA skill registry"
```

---

### Task 5: Pin ADK and isolate MCP capabilities by endpoint

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/src/sre_agent/integrations/mcp/models.py`
- Create: `backend/src/sre_agent/integrations/mcp/client.py`
- Create: `backend/src/sre_agent/integrations/mcp/capability_resolver.py`
- Create: `backend/src/sre_agent/integrations/mcp/factories.py`
- Test: `backend/tests/unit/integrations/mcp/test_capability_resolver.py`
- Test: `backend/tests/contract/mcp/test_endpoint_isolation.py`

**Interfaces:**
- Produces: `McpClient.list_tools()`, `McpClient.call(tool_name, arguments, deadline)`.
- Produces: `CapabilityResolver.resolve(required, manifest, discovered) -> tuple[AllowedTool, ...]`.
- Produces: `McpClientFactory.for_specialist(kind, scope) -> McpClient` with no arbitrary endpoint argument.

- [ ] **Step 1: Write allowlist/isolation RED tests**

Test missing or ambiguous capability fails closed; mutation/unknown annotations reject; endpoint identity must match trusted manifest; Metrics cannot call Log/Trace tools; invalid input schema rejects before network; no scope yields an empty client without connecting.

- [ ] **Step 2: Run RED**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/integrations/mcp tests/contract/mcp -v`

Expected: collection FAIL.

- [ ] **Step 3: Add official Google ADK and MCP dependencies with an exact lock**

Run `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv add google-adk mcp`, commit the resolved `uv.lock`, and never use a floating runtime install. Wrap all SDK imports inside adapter modules.

- [ ] **Step 4: Implement trusted capability manifest and factories**

Manifest entries contain endpoint identity, capabilities, allowed tool-name pattern, input schema hash, and `READ_ONLY`. Production auth uses ADC/Workload Identity; local Bearer is read from injected `SecretProvider` and never stored in `AllowedTool` repr/log.

- [ ] **Step 5: Run tests/static checks and commit**

Run Task 5 tests, Ruff, and Pyright. Expected: PASS/0 errors.

```bash
git add backend/pyproject.toml backend/uv.lock backend/src/sre_agent/integrations/mcp backend/tests/unit/integrations/mcp backend/tests/contract/mcp
git commit -m "feat: isolate RCA MCP capabilities"
```

---

### Task 6: Define specialist and evidence contracts

**Files:**
- Create: `backend/src/sre_agent/domain/evidence/models.py`
- Create: `backend/src/sre_agent/agents/specialists/base.py`
- Create: `backend/src/sre_agent/agents/specialists/metrics_agent.py`
- Create: `backend/src/sre_agent/agents/specialists/trace_agent.py`
- Create: `backend/src/sre_agent/agents/specialists/log_agent.py`
- Test: `backend/tests/unit/agents/specialists/test_contracts.py`

**Interfaces:**
- Produces: `EvidenceDraft`, `EvidenceReference(id: UUID, partition_timestamp: datetime)`, `SpecialistRequest`, `SpecialistResult`.
- Produces: `Specialist.run(request, deadline) -> SpecialistResult`.

- [ ] **Step 1: Write Pydantic contract RED tests**

Reject confidence outside `[0,1]`, naive time, window outside Incident request, endpoint/tool mismatch, raw result not bytes/JSON, claims without evidence, and scope differing from normalized safe scope. Assert `AlertIssue` remains a separate untrusted data field.

- [ ] **Step 2: Run RED**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/agents/specialists/test_contracts.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement immutable contracts and adapters**

Specialists return domain-limited findings only. `EvidenceDraft` contains endpoint identity, capability, tool, input scope, observed/time window, structured JSON, exact raw bytes, content type, and SHA-256 input. No specialist may declare final root cause.

- [ ] **Step 4: Add no-safe-scope behavior**

When `available_tools == ()`, specialist adapters do not instantiate/call MCP and return a deterministic missing-evidence result. Test both blank GCP project and unclassified AWS resource.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/src/sre_agent/domain/evidence backend/src/sre_agent/agents/specialists backend/tests/unit/agents/specialists
git commit -m "feat: define RCA specialist evidence contracts"
```

---

### Task 7: Orchestrate specialists within the five-minute deadline

**Files:**
- Create: `backend/src/sre_agent/agents/rca/models.py`
- Create: `backend/src/sre_agent/agents/rca/workflow.py`
- Test: `backend/tests/unit/agents/rca/test_workflow.py`

**Interfaces:**
- Produces: `RcaWorkflow.run(context: IncidentContext, deadline: datetime) -> InvestigationBundle`.
- Consumes: Task 6 specialists.

- [ ] **Step 1: Write concurrency/deadline RED tests**

Use controllable fake specialists. Assert all eligible specialists start before any completes; no-safe-scope starts none; one timeout preserves other results; global cancellation stops new calls; deadline-before-start returns no invocation; result ordering is deterministic.

- [ ] **Step 2: Run RED**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/agents/rca/test_workflow.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement structured concurrency**

Use `asyncio.TaskGroup`, a global aware-UTC deadline, per-call remaining-time calculation, bounded retry only for transient transport errors, and explicit `SpecialistFailure` values. Re-raise caller cancellation.

- [ ] **Step 4: Run tests and commit**

```bash
git add backend/src/sre_agent/agents/rca backend/tests/unit/agents/rca/test_workflow.py
git commit -m "feat: run RCA specialists within deadline"
```

---

### Task 8: Persist evidence and synthesize cited Traditional Chinese reports

**Files:**
- Create: `backend/src/sre_agent/application/rca/persist_evidence.py`
- Create: `backend/src/sre_agent/persistence/repositories/rca.py`
- Create: `backend/src/sre_agent/domain/rca/models.py`
- Create: `backend/src/sre_agent/agents/rca/synthesizer.py`
- Test: `backend/tests/integration/application/test_persist_evidence.py`
- Test: `backend/tests/unit/agents/rca/test_synthesizer.py`

**Interfaces:**
- Produces: persisted `EvidenceReference(UUID, partition_timestamp)` before synthesis.
- Produces: `RcaReportDraft(status, summary_zh_tw, claims, hypotheses, missing_evidence, verification_steps)`.

- [ ] **Step 1: Write exact evidence RED tests**

Persist bytes containing whitespace/order/duplicate JSON keys and prove exact BYTEA/hash round-trip. Assert JSONB is separately parsed, metadata has endpoint/capability/tool/scope/window, and reports never copy raw evidence.

- [ ] **Step 2: Write hallucination/citation RED tests**

Reject unknown evidence UUID or wrong partition timestamp; reject observed facts without SUPPORTS/CONTRADICTS/MISSING relation; reject COMPLETE when required evidence is absent. No MCP scope must produce PARTIAL with `證據不足` and no invented provider/resource/root cause.

- [ ] **Step 3: Run RED**

Run: `cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/integration/application/test_persist_evidence.py tests/unit/agents/rca/test_synthesizer.py -v`

Expected: collection FAIL.

- [ ] **Step 4: Implement content-addressed persistence and validated synthesis**

Persist evidence first, pass only safe summaries plus opaque references to synthesis, validate the structured result, and allow one corrective retry for schema/citation failures while deadline remains. Status rules: sufficient evidence -> COMPLETE; partial/no safe scope -> PARTIAL; no usable analysis or permanent workflow failure -> FAILED.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/src/sre_agent/application/rca backend/src/sre_agent/persistence/repositories/rca.py backend/src/sre_agent/domain/rca backend/src/sre_agent/agents/rca/synthesizer.py backend/tests
git commit -m "feat: persist evidence-backed RCA reports"
```

---

### Task 9: Implement the idempotent RCA worker and Pub/Sub settlement

**Files:**
- Create: `backend/src/sre_agent/application/rca/job_lifecycle.py`
- Create: `backend/src/sre_agent/workers/rca_worker.py`
- Modify: `backend/pyproject.toml`
- Create: `backend/tests/integration/workers/test_rca_worker.py`
- Create: `backend/tests/integration/pubsub/test_rca_worker_emulator.py`

**Interfaces:**
- Produces: `RcaJobHandler.handle(message: RcaJobMessage) -> JobDisposition`.
- Produces: worker CLI `sre-agent-rca-worker`.

- [ ] **Step 1: Write lifecycle/concurrency RED tests**

Cover valid success, no-scope PARTIAL/no MCP, specialist partial failure, complete failure, duplicate delivery, concurrent consumers, crash after evidence commit, stale 60-second lease recovery, three attempts maximum, mismatched message identifiers, report already terminal, and DB settlement failure.

- [ ] **Step 2: Run RED**

Run: `cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/integration/workers/test_rca_worker.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement atomic claim and lease renewal**

Claim only QUEUED or expired RUNNING rows with `FOR UPDATE SKIP LOCKED`; atomically set owner, `lease_expires_at = now + 60 seconds`, increment attempt, and set `deadline_at = queued_at + 300 seconds`. Lease renewal must compare owner and non-terminal state; lost lease cancels further MCP calls and prevents settlement.

- [ ] **Step 4: Implement durable stage settlement**

Persist attempts, evidence, report, timeline/status, job/run terminal state in recoverable transactions. Ack only after terminal commit. Nack transient failures only if attempt < 3 and deadline remains. Permanently invalid/policy/auth messages record a safe failure code and ack. Never store raw exception text.

- [ ] **Step 5: Test through the official Emulator**

Start PostgreSQL and Emulator, publish a real outbox message, run one delivery, and assert the Pub/Sub message is acked only after report/job commit. Force one nack/redelivery and assert one final report.

Run: `cd backend && PUBSUB_EMULATOR_HOST=127.0.0.1:58085 PUBSUB_PROJECT_ID=sre-agent-test MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/integration/pubsub/test_rca_worker_emulator.py -v`

Expected: PASS.

- [ ] **Step 6: Add the CLI and commit**

Add `sre-agent-rca-worker = "sre_agent.workers.rca_worker:main"`. `main` must return 0/1, close subscriber/DB/MCP resources in `finally`, and never print secrets.

```bash
git add backend/src/sre_agent/application/rca backend/src/sre_agent/workers/rca_worker.py backend/pyproject.toml backend/tests
git commit -m "feat: execute durable RCA jobs"
```

---

### Task 10: Add evaluation datasets and complete release verification

**Files:**
- Create: `backend/tests/eval/datasets/gcp-safe-scope.json`
- Create: `backend/tests/eval/datasets/aws-safe-scope.json`
- Create: `backend/tests/eval/datasets/no-safe-scope.json`
- Create: `backend/tests/eval/test_rca_reports.py`
- Modify: `README.md`
- Modify: `docs/database/postgresql-schema.md`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: deterministic quality/safety gates and local Emulator runbook.

- [ ] **Step 1: Write evaluation RED tests**

Each dataset contains fixed untrusted AlertValues, fixed MCP evidence (or none), expected status, required/forbidden claims, required citations, and zh-TW phrases. Include prompt injection and URL instructions that must not trigger tools.

- [ ] **Step 2: Run RED and implement deterministic fake runtime wiring**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/eval/test_rca_reports.py -v`

Expected initially: FAIL on missing eval composition. Add an injected deterministic runtime only in tests; production remains ADK-backed.

- [ ] **Step 3: Run fresh PostgreSQL and Emulator acceptance**

```bash
docker compose up -d postgres pubsub-emulator
cd backend
POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic downgrade base
POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic upgrade head
PUBSUB_EMULATOR_HOST=127.0.0.1:58085 PUBSUB_PROJECT_ID=sre-agent-test MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests -v
```

Expected: all backend tests PASS.

- [ ] **Step 4: Run contract/static/final safety gates**

```bash
UV_CACHE_DIR=$PWD/.uv-cache uv run --project backend pytest contracts/compatibility-tests -v
cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run ruff check .
cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pyright
git diff --check
test ! -d infrastructure
```

Also scan tracked files for service-account private keys, Authorization values, `POSTGRES_PASSWORD`, and raw AlertValues in Pub/Sub fixtures. Expected: no findings.

- [ ] **Step 5: Update runbook and commit**

Document `docker compose up -d postgres pubsub-emulator`, `PUBSUB_EMULATOR_HOST`, bootstrap, outbox worker, RCA worker, official Emulator limitations, production ADC requirement, 300/60/3 lifecycle, and no Chat scope.

```bash
git add backend/tests/eval README.md docs/database/postgresql-schema.md
git commit -m "test: verify evidence-backed RCA workflow"
```
