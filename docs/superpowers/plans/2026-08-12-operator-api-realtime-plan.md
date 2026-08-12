# Operator API and Realtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose secure, versioned Incident/Alert/RCA/conversation operations and replayable realtime updates for the Angular client.

**Architecture:** Thin FastAPI routers call application use cases through an identity/scope policy boundary. Read models use cursor pagination and explicit DTO mapping. Mutations use ETags or idempotency keys, write audit/timeline/outbox records transactionally, and SSE replays authorized event records.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy async, PostgreSQL 18, SSE, pytest, httpx, OpenAPI 3.1.

## Global Constraints

- Backend authorization is the security boundary; Angular filters are not authorization.
- Identity provider remains pluggable; mock identity is test/local only.
- Production without configured identity denies every operator request.
- Raw payload/evidence requires a distinct elevated permission.
- All list endpoints use cursor pagination and bounded time ranges.
- API error codes are stable English; display text is supplied by Angular zh-TW resources.
- Mutations are audited and concurrency-safe.
- No infrastructure provisioning files.

---

## File map

- `backend/src/sre_agent/integrations/identity/`: provider protocol and local/test adapter.
- `backend/src/sre_agent/policy/authorization.py`: central scope/permission decisions.
- `backend/src/sre_agent/application/incidents/`: commands and query services.
- `backend/src/sre_agent/api/routers/`: v1 operator routes.
- `backend/src/sre_agent/api/schemas/`: contract DTOs, never ORM models.
- `backend/src/sre_agent/application/events/`: replayable authorized event stream.

### Task 1: Pluggable identity and scope authorization

**Files:**
- Create: `backend/src/sre_agent/domain/identity/models.py`
- Create: `backend/src/sre_agent/integrations/identity/provider.py`
- Create: `backend/src/sre_agent/integrations/identity/mock_provider.py`
- Create: `backend/src/sre_agent/policy/authorization.py`
- Create: `backend/tests/unit/policy/test_authorization.py`
- Create: `backend/tests/contract/api/test_identity_fail_closed.py`

**Interfaces:**
- Produces: `IdentityProvider.authenticate(request) -> Subject`.
- Produces: `AuthorizationPolicy.require(subject, permission, resource_scope) -> None`.

- [ ] **Step 1: Write permission matrix tests**

Test team members can read/operate only matching scopes, central SRE grant can span scopes, raw evidence requires `evidence.raw.read`, mapping changes require `classification.manage`, missing identity denies, and mock identity raises during non-local startup.

- [ ] **Step 2: Run failing tests**

Run: `cd backend && uv run pytest tests/unit/policy/test_authorization.py tests/contract/api/test_identity_fail_closed.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement protocols and policy**

Define immutable `Subject(id, external_id, display_name, grants)`, `ResourceScope(team_id, project_id, environment_id, service_id)`, and explicit permission enum. The policy matches grants component-wise and denies incomplete/unclassified scope except to central SRE with `unclassified.manage`.

- [ ] **Step 4: Verify and commit**

Run: `cd backend && uv run pytest tests/unit/policy tests/contract/api/test_identity_fail_closed.py -v`
Expected: PASS.

```bash
git add backend/src/sre_agent/domain/identity backend/src/sre_agent/integrations/identity backend/src/sre_agent/policy backend/tests/unit/policy backend/tests/contract/api/test_identity_fail_closed.py
git commit -m "feat: enforce pluggable scope authorization"
```

### Task 2: Cursor pagination and read DTO mapping

**Files:**
- Create: `backend/src/sre_agent/api/schemas/pagination.py`
- Create: `backend/src/sre_agent/api/schemas/incidents.py`
- Create: `backend/src/sre_agent/api/schemas/alerts.py`
- Create: `backend/src/sre_agent/api/schemas/rca.py`
- Create: `backend/src/sre_agent/application/queries/cursors.py`
- Create: `backend/src/sre_agent/application/queries/incidents.py`
- Create: `backend/src/sre_agent/application/queries/alerts.py`
- Create: `backend/tests/integration/application/queries/test_pagination.py`

**Interfaces:**
- Produces: opaque signed cursor encoding `(sort_time, id, filter_hash)`.
- Produces: contract DTOs matching `operator-api-v1.yaml`.

- [ ] **Step 1: Write stable pagination tests**

Insert equal timestamps and assert UUID tie-break ordering, no duplicate/missing row between pages, cursor rejection when filters change, limit bounds, required `from/to`, and scope predicate inclusion in generated query.

- [ ] **Step 2: Run failing tests**

Run: `cd backend && uv run pytest tests/integration/application/queries/test_pagination.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement opaque cursors and queries**

Encode canonical JSON with URL-safe base64 and HMAC signature from application secret. Query using keyset predicate, apply authorization scope before user filters, map ORM rows to explicit Pydantic DTOs, and return `items/nextCursor`.

- [ ] **Step 4: Verify and commit**

Run: `cd backend && uv run pytest tests/integration/application/queries/test_pagination.py -v`
Expected: PASS.

```bash
git add backend/src/sre_agent/api/schemas backend/src/sre_agent/application/queries backend/tests/integration/application/queries
git commit -m "feat: add authorized cursor query models"
```

### Task 3: Incident commands with optimistic concurrency

**Files:**
- Create: `backend/src/sre_agent/application/incidents/acknowledge_incident.py`
- Create: `backend/src/sre_agent/application/incidents/assign_incident.py`
- Create: `backend/src/sre_agent/application/incidents/resolve_incident.py`
- Create: `backend/src/sre_agent/application/incidents/reopen_incident.py`
- Create: `backend/src/sre_agent/api/routers/incidents.py`
- Create: `backend/tests/contract/api/test_incident_operations.py`

**Interfaces:**
- Consumes: `If-Match: "<version>"`.
- Produces updated Incident DTO and ETag.

- [ ] **Step 1: Write operation tests**

Assert permitted transitions, audit/timeline/outbox writes, actor identity, stale ETag `409 INCIDENT_VERSION_CONFLICT`, unauthorized scope `403 SCOPE_FORBIDDEN`, and successful responses include incremented ETag.

- [ ] **Step 2: Run failing tests**

Run: `cd backend && uv run pytest tests/contract/api/test_incident_operations.py -v`
Expected: FAIL/404.

- [ ] **Step 3: Implement commands**

Each command loads authorized Incident, verifies version, applies domain transition, increments version, and writes status history, timeline, audit, and outbox in one transaction. Assignment records both current assignee and immutable assignment history.

- [ ] **Step 4: Implement router and verify**

Run: `cd backend && uv run pytest tests/contract/api/test_incident_operations.py -v`
Expected: PASS and response schemas validate against OpenAPI.

- [ ] **Step 5: Commit**

```bash
git add backend/src/sre_agent/application/incidents backend/src/sre_agent/api/routers/incidents.py backend/tests/contract/api/test_incident_operations.py
git commit -m "feat: add concurrency-safe incident operations"
```

### Task 4: RCA rerun and shared conversation commands

**Files:**
- Create: `backend/src/sre_agent/application/rca/retry_rca.py`
- Create: `backend/src/sre_agent/application/conversations/add_message.py`
- Create: `backend/src/sre_agent/application/conversations/ask_agent.py`
- Create: `backend/src/sre_agent/api/routers/rca_runs.py`
- Create: `backend/src/sre_agent/api/routers/conversations.py`
- Create: `backend/tests/contract/api/test_rca_and_messages.py`

**Interfaces:**
- RCA mutation consumes `Idempotency-Key`.
- Message command returns persisted message immediately and enqueues an agent-response job.

- [ ] **Step 1: Write idempotency/shared timeline tests**

Assert same idempotency key returns the same run, different key while active returns `409 RCA_ALREADY_RUNNING` with active run ID, completed rerun creates a new version, message order is stable, every message records actor/time, and users cannot edit/delete messages.

- [ ] **Step 2: Run failing tests**

Run: `cd backend && uv run pytest tests/contract/api/test_rca_and_messages.py -v`
Expected: FAIL/404.

- [ ] **Step 3: Implement commands and routes**

Rerun inherits Incident scope and creates queued/outbox records atomically. Unclassified Incident creates/returns a waiting run. User message stores `USER`, timeline/audit/outbox; the later worker stores `AGENT` with related RCA run and evidence IDs.

- [ ] **Step 4: Verify and commit**

Run: `cd backend && uv run pytest tests/contract/api/test_rca_and_messages.py -v`
Expected: PASS.

```bash
git add backend/src/sre_agent/application/rca/retry_rca.py backend/src/sre_agent/application/conversations backend/src/sre_agent/api/routers/rca_runs.py backend/src/sre_agent/api/routers/conversations.py backend/tests/contract/api/test_rca_and_messages.py
git commit -m "feat: add RCA rerun and shared investigation API"
```

### Task 5: Alert classification and mapping API

**Files:**
- Create: `backend/src/sre_agent/application/alerts/preview_mapping.py`
- Create: `backend/src/sre_agent/application/alerts/manage_mapping.py`
- Create: `backend/src/sre_agent/api/routers/alerts.py`
- Create: `backend/src/sre_agent/api/routers/mappings.py`
- Create: `backend/tests/contract/api/test_alert_classification.py`

**Interfaces:**
- Produces unclassified list, classification mutation, mapping CRUD, and match preview.

- [ ] **Step 1: Write classification API tests**

Assert only `unclassified.manage` can see unclassified alerts, classification releases waiting RCA, reusable mapping is audited, preview lists bounded existing matches without mutation, broad empty matcher is rejected, and mapping update uses ETag.

- [ ] **Step 2: Run failing tests**

Run: `cd backend && uv run pytest tests/contract/api/test_alert_classification.py -v`
Expected: FAIL/404.

- [ ] **Step 3: Implement preview/CRUD routes**

Require at least source plus one rule/folder/label matcher. Preview returns count and first bounded summaries. CRUD writes audit/outbox and preserves priority ordering. Delete is soft-disable to preserve historical explanation.

- [ ] **Step 4: Verify and commit**

Run: `cd backend && uv run pytest tests/contract/api/test_alert_classification.py -v`
Expected: PASS.

```bash
git add backend/src/sre_agent/application/alerts backend/src/sre_agent/api/routers/alerts.py backend/src/sre_agent/api/routers/mappings.py backend/tests/contract/api/test_alert_classification.py
git commit -m "feat: expose alert classification and mappings"
```

### Task 6: Replayable authorized SSE

**Files:**
- Create: `backend/src/sre_agent/application/events/stream_events.py`
- Create: `backend/src/sre_agent/api/routers/event_stream.py`
- Create: `backend/tests/contract/api/test_event_stream.py`

**Interfaces:**
- Produces: `GET /api/v1/events/stream` with `id`, `event`, and JSON `data` frames.

- [ ] **Step 1: Write stream/replay tests**

Test live delivery, `Last-Event-ID` or equivalent `after` cursor replay, heartbeat comments, authorization filtering, revoked scope on reconnect, unknown/expired cursor problem response, ordering by `(occurred_at,id)`, and disconnect cancellation without leaked DB sessions.

- [ ] **Step 2: Run failing tests**

Run: `cd backend && uv run pytest tests/contract/api/test_event_stream.py -v`
Expected: FAIL/404.

- [ ] **Step 3: Implement event reader**

Read immutable timeline/outbox-backed operator events with keyset cursor. Accept browser replay through `after=<eventId>` and standard clients through `Last-Event-ID`, rejecting conflicting values. Apply current subject scope on every batch. Serialize only `IncidentEventV1` fields; never include raw evidence. Emit heartbeat comments while idle and close cleanly on cancellation.

- [ ] **Step 4: Verify and commit**

Run: `cd backend && uv run pytest tests/contract/api/test_event_stream.py -v`
Expected: PASS.

```bash
git add backend/src/sre_agent/application/events backend/src/sre_agent/api/routers/event_stream.py backend/tests/contract/api/test_event_stream.py
git commit -m "feat: stream authorized replayable incident events"
```

### Task 7: Dashboard, RCA, evidence, timeline, and audit read routes

**Files:**
- Create: `backend/src/sre_agent/application/queries/dashboard.py`
- Create: `backend/src/sre_agent/application/queries/rca.py`
- Create: `backend/src/sre_agent/application/queries/timeline.py`
- Create: `backend/src/sre_agent/api/routers/dashboard.py`
- Create: `backend/src/sre_agent/api/routers/read_models.py`
- Create: `backend/tests/contract/api/test_operator_reads.py`

**Interfaces:**
- Produces all approved dashboard, Incident detail/timeline, RCA report/evidence/hypothesis, messages, audit, and protected raw-data GET operations.

- [ ] **Step 1: Write contract and authorization tests**

Assert every GET response validates against OpenAPI, dashboard counts honor scope/filters, report versions are immutable, claims link to evidence, raw endpoints require `evidence.raw.read`, sensitive keys are redacted, and large JSON is returned only from the explicit raw endpoint.

- [ ] **Step 2: Run failing tests**

Run: `cd backend && uv run pytest tests/contract/api/test_operator_reads.py -v`
Expected: FAIL/404.

- [ ] **Step 3: Implement query services and routes**

Apply scope authorization in the query before loading rows. Return explicit DTOs, cursor pages for long collections, current and historical RCA versions, provenance references, and bounded dashboard aggregation windows. Raw reads create an audit record in the same request transaction.

- [ ] **Step 4: Verify and commit**

Run: `cd backend && uv run pytest tests/contract/api/test_operator_reads.py -v`
Expected: PASS.

```bash
git add backend/src/sre_agent/application/queries backend/src/sre_agent/api/routers/dashboard.py backend/src/sre_agent/api/routers/read_models.py backend/tests/contract/api/test_operator_reads.py
git commit -m "feat: expose authorized operator read models"
```

### Task 8: Full Operator API contract and security verification

**Files:**
- Modify: `backend/src/sre_agent/api/main.py`
- Create: `backend/tests/contract/api/test_operator_openapi.py`
- Create: `backend/tests/integration/security/test_scope_isolation.py`

**Interfaces:**
- Produces complete `/api/v1` contract implementation.

- [ ] **Step 1: Mount all v1 routers and test OpenAPI operation coverage**

Compare FastAPI operation path/method pairs to `contracts/openapi/operator-api-v1.yaml`; fail on missing or undocumented public operations. Exclude only health/readiness endpoints from the comparison.

- [ ] **Step 2: Add adversarial scope tests**

Create two teams and test list, detail, timeline, RCA, message, SSE, and raw evidence paths. Manipulating `teamId`, Incident UUID, cursor, or raw endpoint must never reveal the other team.

- [ ] **Step 3: Run final gates**

Run: `cd backend && uv run pytest tests/contract/api tests/integration/security -v && uv run ruff check . && uv run pyright`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/src/sre_agent/api/main.py backend/tests/contract/api/test_operator_openapi.py backend/tests/integration/security
git commit -m "test: verify operator contract and scope isolation"
```
