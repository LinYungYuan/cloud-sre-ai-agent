# Grafana Ingestion and PostgreSQL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert authenticated Grafana webhooks into durable, deduplicated Alert/Incident records and exactly one queued RCA job.

**Architecture:** FastAPI delegates to an application service that runs one PostgreSQL transaction: immutable delivery, normalized events, current state, classification, Incident, RCA run, worker job, and outbox event. SQLAlchemy adapters implement domain repository protocols; Pub/Sub publishing happens only from the outbox worker after commit.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2 async, asyncpg, Alembic, PostgreSQL 18, pytest, pytest-asyncio, httpx.

## Global Constraints

- Valid webhook transaction returns `202` within two seconds.
- New Incident is queryable within five seconds.
- All accepted raw payloads are retained permanently in PostgreSQL 18.
- Volume assumption is fewer than 1,000 alert instances/day.
- Authentication failures are not persisted.
- Unknown scope creates `WAITING_FOR_CLASSIFICATION`; it must not query MCP.
- Grafana `resolved` never automatically resolves the human Incident.
- No infrastructure provisioning files.

---

## File map

- `backend/src/sre_agent/persistence/models/`: SQLAlchemy persistence-only models.
- `backend/migrations/versions/`: PostgreSQL schema, constraints, partitions, indexes.
- `backend/src/sre_agent/integrations/grafana/`: authentication, payload parsing, normalization.
- `backend/src/sre_agent/domain/alerts/fingerprint.py`: deterministic dedup identity.
- `backend/src/sre_agent/domain/alerts/classification.py`: labels/mapping resolution.
- `backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py`: transaction coordinator.
- `backend/src/sre_agent/api/routers/grafana_webhook.py`: thin HTTP boundary.
- `backend/src/sre_agent/workers/outbox_worker.py`: publish-after-commit loop.

### Task 1: PostgreSQL 18 schema and partitions

**Files:**
- Create: `backend/alembic.ini`
- Create: `backend/migrations/env.py`
- Create: `backend/migrations/versions/0001_alert_incident_schema.py`
- Create: `backend/src/sre_agent/persistence/database.py`
- Create: `backend/src/sre_agent/persistence/models/*.py`
- Create: `backend/tests/integration/persistence/test_schema.py`
- Create: `docker-compose.yml`

**Interfaces:**
- Produces tables named in design section 9, UUID primary keys, UTC `TIMESTAMPTZ`, JSONB raw payloads, and monthly partitions.

- [ ] **Step 1: Start PostgreSQL 18 for local tests**

Create a root `docker-compose.yml` service using image `postgres:18`, database `sre_agent`, a healthcheck with `pg_isready`, and a named local volume. Do not add cloud or Kubernetes configuration.

- [ ] **Step 2: Write failing migration assertions**

The async integration test queries `pg_class`, `pg_partitioned_table`, `pg_indexes`, and `information_schema.columns`. Assert that `webhook_deliveries`, `alert_events`, `evidence_records`, `incident_messages`, `incident_timeline_events`, and `audit_events` are range-partitioned; `incidents` is not; all lifecycle timestamps are `timestamp with time zone`.

- [ ] **Step 3: Run before migration**

Run: `docker compose up -d postgres && cd backend && uv run alembic upgrade head && uv run pytest tests/integration/persistence/test_schema.py -v`
Expected before implementing revision: FAIL because revision/table definitions are missing.

- [ ] **Step 4: Implement migration**

Create the approved tables with foreign keys and checks. Logical IDs are UUIDs. Because PostgreSQL requires a partitioned table's unique/primary key to include every partition key, each monthly table uses composite primary key `(id, partition_timestamp)`; any relational reference to a partitioned row stores both columns. Cross-partition deduplication lives in unpartitioned `ingestion_dedup_keys`/content-address records. Add partial unique index on `rca_runs(incident_id) WHERE status IN ('WAITING_FOR_CLASSIFICATION','QUEUED','RUNNING')`; unique `ingestion_dedup_keys(source_id, dedup_key)`; unique `alert_instances(source_id, fingerprint)`; unique `outbox_events(idempotency_key)`. Create current and next monthly partitions transactionally; later months are created by `partition_worker`.

- [ ] **Step 5: Add partition creation function**

Implement `ensure_monthly_partitions(connection, month: date) -> None` using allowlisted table names and bound start/end dates. Test it is idempotent and creates the correct exclusive upper bound.

- [ ] **Step 6: Verify and commit**

Run: `cd backend && uv run alembic downgrade base && uv run alembic upgrade head && uv run pytest tests/integration/persistence/test_schema.py -v`
Expected: PASS.

```bash
git add backend/alembic.ini backend/migrations backend/src/sre_agent/persistence backend/tests/integration/persistence docker-compose.yml
git commit -m "feat: add PostgreSQL alert and incident schema"
```

### Task 2: Grafana Bearer authentication and raw payload boundary

**Files:**
- Create: `backend/src/sre_agent/integrations/grafana/authenticator.py`
- Create: `backend/src/sre_agent/integrations/grafana/payloads.py`
- Create: `backend/tests/unit/integrations/grafana/test_authenticator.py`
- Create: `backend/tests/unit/integrations/grafana/test_payloads.py`

**Interfaces:**
- Produces: `GrafanaTokenAuthenticator.verify(source_id: UUID, authorization: str | None) -> str` returning a non-secret token identifier.
- Produces: `parse_grafana_body(raw_body: bytes, max_bytes: int) -> GrafanaWebhook`.

- [ ] **Step 1: Write failing security tests**

Test missing/wrong schemes, invalid token, valid current token, valid rotation token, constant-time comparison, invalid JSON, and body over 1 MiB. Assert exception messages never contain supplied credentials or raw body.

- [ ] **Step 2: Run and confirm failure**

Run: `cd backend && uv run pytest tests/unit/integrations/grafana -v`
Expected: FAIL with missing modules.

- [ ] **Step 3: Implement authenticator**

Inject `SecretProvider.get_grafana_tokens(source_id) -> Mapping[str, SecretStr]`. Parse exactly `Bearer <credential>`, compare every configured value with `hmac.compare_digest`, and return its token ID. Define `GrafanaUnauthorized`, `GrafanaPayloadTooLarge`, and `GrafanaPayloadInvalid` without sensitive attributes.

- [ ] **Step 4: Implement Pydantic payloads**

Model all Grafana v1 contract fields, retain unknown data with `extra='allow'`, parse dates as aware UTC, and retain `raw_body` separately for hashing/persistence. Never reserialize the Pydantic object as the immutable raw payload.

- [ ] **Step 5: Verify and commit**

Run: `cd backend && uv run pytest tests/unit/integrations/grafana -v && uv run ruff check .`
Expected: PASS.

```bash
git add backend/src/sre_agent/integrations/grafana backend/tests/unit/integrations/grafana
git commit -m "feat: authenticate and parse Grafana webhooks"
```

### Task 3: Normalization, fingerprint, and classification

**Files:**
- Create: `backend/src/sre_agent/integrations/grafana/normalizer.py`
- Create: `backend/src/sre_agent/domain/alerts/fingerprint.py`
- Create: `backend/src/sre_agent/domain/alerts/classification.py`
- Create: `backend/tests/unit/domain/alerts/test_fingerprint.py`
- Create: `backend/tests/unit/domain/alerts/test_classification.py`
- Create: `backend/tests/unit/integrations/grafana/test_normalizer.py`

**Interfaces:**
- Produces: `normalize_alerts(source_id, webhook) -> tuple[CanonicalAlertEvent, ...]`.
- Produces: `make_dedup_key(source_id, fingerprint, status, starts_at, ends_at, raw_sha256) -> str`.
- Produces: `AlertClassifier.classify(labels, rule_uid, folder) -> ClassificationResult`.

- [ ] **Step 1: Write deterministic fixture tests**

Assert grouped payloads expand to one event per `alerts[]`, source ID participates in identity, label order does not affect fallback fingerprint, exact labels override mappings, lowest numeric mapping priority wins, and no match returns `UNCLASSIFIED` with the missing fields.

- [ ] **Step 2: Run the failing tests**

Run: `cd backend && uv run pytest tests/unit/domain/alerts tests/unit/integrations/grafana/test_normalizer.py -v`
Expected: FAIL with missing functions.

- [ ] **Step 3: Implement identity rules**

Use Grafana fingerprint when non-empty. Otherwise compute SHA-256 over canonical JSON containing sorted labels and source ID. Compute delivery body hash from the exact raw bytes. Dedup key includes lifecycle identity so a later resolved event is not discarded as a firing duplicate.

- [ ] **Step 4: Implement mapping matchers**

Support exact `source_id`, optional exact `rule_uid`/`folder`, and a map of required exact label pairs. Sort enabled rules by `(priority, created_at, id)`. Return label-derived scope only when all `team/project/environment/service` labels resolve to known records; otherwise fill missing values from the winning mapping.

- [ ] **Step 5: Verify and commit**

Run: `cd backend && uv run pytest tests/unit/domain/alerts tests/unit/integrations/grafana/test_normalizer.py -v`
Expected: PASS.

```bash
git add backend/src/sre_agent/domain/alerts backend/src/sre_agent/integrations/grafana/normalizer.py backend/tests/unit/domain/alerts backend/tests/unit/integrations/grafana/test_normalizer.py
git commit -m "feat: normalize and classify Grafana alerts"
```

### Task 4: Transactional ingestion use case

**Files:**
- Create: `backend/src/sre_agent/persistence/unit_of_work.py`
- Create: `backend/src/sre_agent/persistence/repositories/alerts.py`
- Create: `backend/src/sre_agent/persistence/repositories/incidents.py`
- Create: `backend/src/sre_agent/persistence/repositories/jobs.py`
- Create: `backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py`
- Create: `backend/tests/integration/application/test_ingest_grafana_alerts.py`

**Interfaces:**
- Consumes: authentication result, raw bytes, normalized events, classifier.
- Produces: `IngestionResult(delivery_id: UUID, accepted_at: datetime, incident_ids: tuple[UUID, ...])`.

- [ ] **Step 1: Write integration tests for atomic behavior**

Cover new firing, identical redelivery, firing update, resolved, new firing after manually resolved Incident, grouped alerts, and unclassified alert. Assert exactly one active RCA run and one outbox event per new Incident; unclassified run status is `WAITING_FOR_CLASSIFICATION`; classified run is `QUEUED`; resolved does not change Incident status.

- [ ] **Step 2: Run against migrated PostgreSQL**

Run: `cd backend && uv run pytest tests/integration/application/test_ingest_grafana_alerts.py -v`
Expected: FAIL because use case/repositories are missing.

- [ ] **Step 3: Implement UnitOfWork and repositories**

Define async protocols and SQLAlchemy implementations. Use `INSERT ... ON CONFLICT` for dedup/current alert state. Lock an active Incident candidate using `SELECT ... FOR UPDATE`; rely on partial unique indexes as the final concurrency guard. All writes, including outbox, occur on the same async session transaction.

- [ ] **Step 4: Implement ingestion orchestration**

`IngestGrafanaAlerts.execute(source_id, token_id, raw_body, received_at)` parses/normalizes and persists. For every new firing Incident, create `rca_runs`, `worker_jobs`, and `outbox_events` with idempotency key `rca-run:{run_id}`. On duplicate dedup key, preserve a delivery record with status `DUPLICATE` but do not repeat state transition writes.

- [ ] **Step 5: Prove rollback**

Inject a repository failure after Incident insertion and assert delivery, Incident, RCA job, and outbox are all absent after rollback.

- [ ] **Step 6: Verify and commit**

Run: `cd backend && uv run pytest tests/integration/application/test_ingest_grafana_alerts.py -v`
Expected: PASS.

```bash
git add backend/src/sre_agent/application backend/src/sre_agent/persistence backend/tests/integration/application
git commit -m "feat: ingest alerts atomically with RCA outbox"
```

### Task 5: Thin FastAPI webhook endpoint

**Files:**
- Create: `backend/src/sre_agent/api/main.py`
- Create: `backend/src/sre_agent/api/dependencies.py`
- Create: `backend/src/sre_agent/api/error_handlers.py`
- Create: `backend/src/sre_agent/api/middleware/correlation_id.py`
- Create: `backend/src/sre_agent/api/routers/grafana_webhook.py`
- Create: `backend/tests/contract/api/test_grafana_webhook.py`

**Interfaces:**
- Produces: contract-compliant `POST /webhooks/v1/grafana/{sourceId}`.

- [ ] **Step 1: Write HTTP contract tests**

Use `httpx.AsyncClient` with dependency overrides. Assert `202` body, correlation header, `400/401/413/500` problem bodies, accepted content type, and that invalid auth never calls ingestion. Add a latency test with fake repositories that must finish under two seconds.

- [ ] **Step 2: Run and confirm 404/failure**

Run: `cd backend && uv run pytest tests/contract/api/test_grafana_webhook.py -v`
Expected: FAIL because app/router do not exist.

- [ ] **Step 3: Implement endpoint**

Read exact raw bytes once, enforce `Content-Type: application/json`, authenticate before persistence, call the application service, and return `WebhookAccepted`. Map domain errors to `application/problem+json`; unexpected errors return a generic message plus correlation ID without payload/token details.

- [ ] **Step 4: Verify contract and security**

Run: `cd backend && uv run pytest tests/contract/api/test_grafana_webhook.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/sre_agent/api backend/tests/contract/api
git commit -m "feat: expose authenticated Grafana webhook"
```

### Task 6: Outbox publisher and classification release

**Files:**
- Create: `backend/src/sre_agent/integrations/pubsub/publisher.py`
- Create: `backend/src/sre_agent/workers/outbox_worker.py`
- Create: `backend/src/sre_agent/application/alerts/classify_alert.py`
- Create: `backend/tests/integration/workers/test_outbox_worker.py`
- Create: `backend/tests/integration/application/test_classify_alert.py`

**Interfaces:**
- Produces: `OutboxPublisher.publish_batch(limit: int) -> int`.
- Produces: `ClassifyAlert.execute(alert_id, scope, create_mapping, actor) -> UUID` returning released RCA run ID.

- [ ] **Step 1: Write redelivery and classification tests**

Assert publisher marks an event published only after Pub/Sub acknowledgement, retries failures with backoff metadata, and publishing the same outbox record twice uses the same message idempotency key. Assert classification updates scope, writes audit/timeline, and atomically changes the waiting RCA run to `QUEUED` plus outbox event.

- [ ] **Step 2: Run failing tests**

Run: `cd backend && uv run pytest tests/integration/workers/test_outbox_worker.py tests/integration/application/test_classify_alert.py -v`
Expected: FAIL with missing implementations.

- [ ] **Step 3: Implement publisher boundary**

Define `MessagePublisher.publish(topic: str, data: bytes, attributes: Mapping[str, str]) -> str`; provide Google Pub/Sub and fake implementations. Lock unpublished rows with `FOR UPDATE SKIP LOCKED`, publish canonical JSON containing `eventId`, `eventType`, `resourceId`, `occurredAt`, `version`, then update published timestamp.

- [ ] **Step 4: Implement classification release**

Require complete valid scope. Update the alert/Incident scope, optionally insert mapping, write audit and timeline, transition exactly one active run from `WAITING_FOR_CLASSIFICATION` to `QUEUED`, and enqueue `rca-run:{run_id}`.

- [ ] **Step 5: Final ingestion verification and commit**

Run: `cd backend && uv run pytest tests/unit tests/integration tests/contract/api/test_grafana_webhook.py -v && uv run ruff check . && uv run pyright`
Expected: PASS.

```bash
git add backend/src/sre_agent/integrations/pubsub backend/src/sre_agent/workers backend/src/sre_agent/application/alerts/classify_alert.py backend/tests
git commit -m "feat: publish durable jobs and release classified alerts"
```
