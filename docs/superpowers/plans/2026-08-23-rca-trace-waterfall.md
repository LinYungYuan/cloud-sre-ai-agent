# RCA Trace Waterfall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe, built-in waterfall for one representative Trace to the RCA section of an Incident detail page.

**Architecture:** The RCA Worker normalizes provider-specific Trace MCP results into a versioned, provider-neutral structure stored in `evidence_records.structured_data`. The Backend authorizes access through the owning Incident and exposes only the normalized allowlisted projection. Angular loads that projection independently of the RCA report and renders a keyboard-accessible tree/timeline with isolated loading, empty, and error states.

**Tech Stack:** Python 3.11+, Pydantic, SQLAlchemy async, FastAPI, PostgreSQL JSONB, OpenAPI 3.1, Angular 22 standalone components, RxJS 7.8, Vitest/jsdom.

**Spec:** `docs/superpowers/specs/2026-08-23-rca-trace-waterfall-design.md`

## Global Constraints

- Show exactly one representative Trace per RCA run in version 1.
- Persist normalized Trace data in `evidence_records.structured_data`; add no table and no migration.
- Return no `raw_result`, HTTP body, SQL statement/parameters, authorization, cookie, token, arbitrary baggage, or personal data to the Frontend.
- Return at most 100 displayed Spans, preserving critical-path Spans, error Spans, and their ancestors before other Spans.
- The Trace panel must fail independently; Incident and RCA report rendering must remain usable.
- Frontend runtime requires Node.js `>=24.15.0 <25` and npm `11.12.1`.
- All Backend response fields use camelCase and all OpenAPI object schemas set `additionalProperties: false`.
- Do not add multi-Trace selection, zoom, search, export, complex filters, or direct Grafana/Tempo access.

---

## File Structure

### RCA Worker

- Create `rca-worker/src/sre_rca_worker/domain/evidence/trace_waterfall.py`: versioned normalized Trace/Span models, attribute allowlist, representative selection, and truncation.
- Modify `rca-worker/src/sre_rca_worker/agents/specialists/base.py`: add a protected normalization hook while preserving Metrics and Logs behavior.
- Modify `rca-worker/src/sre_rca_worker/agents/specialists/trace_agent.py`: invoke the Trace normalizer for Trace MCP results.
- Create `rca-worker/tests/unit/domain/evidence/test_trace_waterfall.py`: deterministic normalization, security, representative selection, and truncation tests.
- Modify `rca-worker/tests/unit/agents/specialists/test_contracts.py`: prove Trace evidence stores normalized JSON while preserving exact raw bytes.

### Backend

- Create `backend/src/sre_agent/application/operator/trace_waterfall.py`: strict Pydantic validation and safe snake_case read projection.
- Modify `backend/src/sre_agent/application/operator/read_models.py`: add `get_trace_waterfall` to available/unavailable read-service interfaces.
- Modify `backend/src/sre_agent/persistence/repositories/operator_reads.py`: authorized Trace evidence query and safe projection.
- Modify `backend/src/sre_agent/api/schemas/operator.py`: HTTP response schemas.
- Modify `backend/src/sre_agent/api/routers/operator_incidents.py`: `GET /api/v1/rca-runs/{id}/trace-waterfall`.
- Modify `backend/tests/integration/api/test_operator_read_repository.py`: persisted valid, missing, malformed, and unauthorized evidence cases.
- Modify `backend/tests/contract/api/test_operator_reads.py`: route, serialization, and error-isolation contract tests.

### Contracts

- Modify `contracts/openapi/operator-api-v1.yaml`: path and strict Trace waterfall schemas.
- Create `contracts/examples/trace-waterfall-response.json`: canonical five-Span response.
- Modify `contracts/compatibility-tests/test_contracts.py`: path/response-schema and example validation assertions.

### Frontend

- Modify `frontend/src/app/core/api/operator-api.models.ts`: Trace response/state types.
- Modify `frontend/src/app/core/api/operator-api.client.ts`: typed waterfall request.
- Modify `frontend/src/app/core/api/operator-api.client.spec.ts`: encoded URL and response test.
- Create `frontend/src/app/features/rca/trace-waterfall.component.ts`: pure waterfall visualization and selection interaction.
- Create `frontend/src/app/features/rca/trace-waterfall.component.spec.ts`: layout, accessibility, interaction, and state tests.
- Modify `frontend/src/app/features/rca/rca-report.component.ts`: place waterfall between impact and recommendations.
- Modify `frontend/src/app/features/rca/rca-report.component.spec.ts`: nested panel integration and retry event.
- Modify `frontend/src/app/features/incidents/incident-detail.component.ts`: independently load/retry waterfall state.
- Create `frontend/src/app/features/incidents/incident-detail.component.spec.ts`: Trace failure must not suppress the report.

---

### Task 1: Normalize and Select Representative Trace Evidence

**Files:**
- Create: `rca-worker/src/sre_rca_worker/domain/evidence/trace_waterfall.py`
- Create: `rca-worker/tests/unit/domain/evidence/test_trace_waterfall.py`

**Interfaces:**
- Consumes: decoded MCP JSON as `dict[str, Any] | list[Any]` and the Incident alert issue as `str`.
- Produces: `normalize_trace_evidence(payload: dict[str, Any] | list[Any], *, alert_issue: str, max_spans: int = 100) -> dict[str, Any] | None`.
- Output keys are exactly `schemaVersion`, `traceId`, `rootServiceName`, `rootOperationName`, `startedAt`, `durationMs`, `spanCount`, `representativeScore`, `truncated`, and `spans`.

- [ ] **Step 1: Write failing model, security, selection, and truncation tests**

Use a fixed OTLP-like payload with two candidate Traces. The slower error Trace must win; sensitive attributes must be absent; a shuffled Span list must produce parent-before-child output.

```python
def test_normalizes_and_selects_error_trace_without_sensitive_attributes() -> None:
    payload = {
        "traces": [
            {
                "traceId": "ok-trace",
                "startedAt": "2026-08-23T04:20:00Z",
                "spans": [
                    {
                        "spanId": "ok-root",
                        "parentSpanId": None,
                        "serviceName": "checkout-api",
                        "operationName": "POST /checkout",
                        "startOffsetMs": 0,
                        "durationMs": 220,
                        "status": "OK",
                        "kind": "SERVER",
                        "criticalPath": True,
                        "attributes": {"http.request.method": "POST"},
                    }
                ],
            },
            {
                "traceId": "error-trace",
                "startedAt": "2026-08-23T04:21:00Z",
                "latencyAnomalyScore": 0.96,
                "spans": [
                    {
                        "spanId": "db",
                        "parentSpanId": "root",
                        "serviceName": "checkout-api",
                        "operationName": "db.connection.acquire",
                        "startOffsetMs": 320,
                        "durationMs": 1480,
                        "status": "ERROR",
                        "kind": "INTERNAL",
                        "criticalPath": True,
                        "attributes": {
                            "db.system": "postgresql",
                            "db.statement": "SELECT secret FROM users",
                            "authorization": "Bearer secret",
                        },
                    },
                    {
                        "spanId": "root",
                        "parentSpanId": None,
                        "serviceName": "checkout-api",
                        "operationName": "POST /checkout",
                        "startOffsetMs": 0,
                        "durationMs": 1925,
                        "status": "ERROR",
                        "kind": "SERVER",
                        "criticalPath": True,
                        "attributes": {"http.response.status_code": 500},
                    },
                ],
            },
        ]
    }

    result = normalize_trace_evidence(payload, alert_issue="checkout latency")

    assert result is not None
    assert result["traceId"] == "error-trace"
    assert [span["spanId"] for span in result["spans"]] == ["root", "db"]
    assert result["spans"][1]["attributes"] == {"db.system": "postgresql"}
```

Add a 105-Span fixture where one late error Span and all its ancestors survive `max_spans=100`, `spanCount == 105`, and `truncated is True`. Add invalid fixtures for negative duration, duplicate Span ID, missing parent, unsupported status, and non-object attributes; each must return `None` rather than partially trusted output.

- [ ] **Step 2: Run the new unit tests and verify they fail**

Run:

```bash
cd rca-worker
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest tests/unit/domain/evidence/test_trace_waterfall.py -v
```

Expected: collection fails because `sre_rca_worker.domain.evidence.trace_waterfall` does not exist.

- [ ] **Step 3: Implement strict normalized models and deterministic selection**

Implement frozen Pydantic models with `extra="forbid"`. Accept candidate lists at `payload["traces"]`, `payload["data"]["traces"]`, or a single object containing `spans`. Normalize common camelCase/snake_case aliases before model validation.

```python
ALLOWED_ATTRIBUTES = frozenset(
    {
        "http.request.method",
        "http.response.status_code",
        "rpc.system",
        "rpc.service",
        "rpc.method",
        "db.system",
        "db.operation.name",
        "server.address",
        "server.port",
    }
)

def normalize_trace_evidence(
    payload: dict[str, Any] | list[Any],
    *,
    alert_issue: str,
    max_spans: int = 100,
) -> dict[str, Any] | None:
    candidates = tuple(_parse_candidates(payload))
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda item: (
            item.has_error,
            item.latency_anomaly_score,
            _issue_match_score(item, alert_issue),
            item.duration_ms,
            item.trace_id,
        ),
        reverse=True,
    )
    selected = _truncate(ranked[0], max_spans=max_spans)
    return selected.to_storage_dict()
```

`_truncate` must collect critical-path IDs, error IDs, and their ancestor closure first, then add remaining Spans ordered by `(start_offset_ms, span_id)`. `_topological_order` must emit a parent before its children and sort siblings by the same stable tuple. Reject a candidate if any Span has an invalid parent, duplicate ID, negative time, or extends beyond `durationMs` by more than 1ms rounding tolerance.

- [ ] **Step 4: Run the focused unit tests**

Run the command from Step 2. Expected: all tests pass.

- [ ] **Step 5: Run Worker static checks**

```bash
cd rca-worker
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check src/sre_rca_worker/domain/evidence/trace_waterfall.py tests/unit/domain/evidence/test_trace_waterfall.py
UV_CACHE_DIR="$PWD/.uv-cache" uv run pyright src/sre_rca_worker/domain/evidence/trace_waterfall.py
```

Expected: both commands exit 0.

- [ ] **Step 6: Commit Task 1**

```bash
git add -- rca-worker/src/sre_rca_worker/domain/evidence/trace_waterfall.py rca-worker/tests/unit/domain/evidence/test_trace_waterfall.py
git commit -m "feat(worker): normalize trace waterfall evidence"
```

---

### Task 2: Apply Trace Normalization in the Specialist

**Files:**
- Modify: `rca-worker/src/sre_rca_worker/agents/specialists/base.py`
- Modify: `rca-worker/src/sre_rca_worker/agents/specialists/trace_agent.py`
- Modify: `rca-worker/tests/unit/agents/specialists/test_contracts.py`

**Interfaces:**
- Consumes: Task 1 `normalize_trace_evidence(payload, *, alert_issue, max_spans=100)`.
- Produces: `McpSpecialist._normalize_structured(structured, request)` hook and Trace evidence whose `structured_json` is the normalized waterfall object.
- Metrics and Logs continue to persist decoded MCP JSON unchanged.

- [ ] **Step 1: Add a failing Trace specialist contract test**

Use the same `AllowedTool` pattern as the Metrics test, but return a Trace payload and assert both normalized JSON and exact raw bytes.

```python
result = await TraceSpecialist(Client).run(request, NOW + timedelta(minutes=1))
evidence = result.findings[0].evidence[0]
assert evidence.structured_json["schemaVersion"] == 1
assert evidence.structured_json["traceId"] == "trace-1"
assert evidence.raw_result == raw
```

Add a malformed Trace test where normalization returns no trusted object. The specialist must return no finding and append `INVALID_TRACE_EVIDENCE` to `missing_evidence`.

- [ ] **Step 2: Run the two new specialist tests and verify failure**

```bash
cd rca-worker
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest tests/unit/agents/specialists/test_contracts.py -k trace -v
```

Expected: failure because Trace evidence remains provider-specific.

- [ ] **Step 3: Add the normalization hook without duplicating `run`**

In `McpSpecialist`, call the hook after decoding JSON and before creating `EvidenceDraft`:

```python
normalized = self._normalize_structured(structured, request)
if normalized is None:
    missing.append("INVALID_TRACE_EVIDENCE")
    continue
structured = normalized
```

The base implementation returns the input unchanged. Override only in `TraceSpecialist`:

```python
def _normalize_structured(self, structured, request):  # type: ignore[no-untyped-def]
    return normalize_trace_evidence(structured, alert_issue=request.alert_issue)
```

Keep `raw_result=raw` unchanged so persistence preserves audit evidence exactly.

- [ ] **Step 4: Run specialist and normalizer tests**

```bash
cd rca-worker
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest tests/unit/agents/specialists/test_contracts.py tests/unit/domain/evidence/test_trace_waterfall.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Run Worker lint and type checks for changed files**

```bash
cd rca-worker
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check src/sre_rca_worker/agents/specialists/base.py src/sre_rca_worker/agents/specialists/trace_agent.py tests/unit/agents/specialists/test_contracts.py
UV_CACHE_DIR="$PWD/.uv-cache" uv run pyright src/sre_rca_worker/agents/specialists/base.py src/sre_rca_worker/agents/specialists/trace_agent.py
```

Expected: both commands exit 0.

- [ ] **Step 6: Commit Task 2**

```bash
git add -- rca-worker/src/sre_rca_worker/agents/specialists/base.py rca-worker/src/sre_rca_worker/agents/specialists/trace_agent.py rca-worker/tests/unit/agents/specialists/test_contracts.py
git commit -m "feat(worker): persist normalized trace spans"
```

---

### Task 3: Add the Authorized Backend Trace Projection

**Files:**
- Create: `backend/src/sre_agent/application/operator/trace_waterfall.py`
- Modify: `backend/src/sre_agent/application/operator/read_models.py`
- Modify: `backend/src/sre_agent/persistence/repositories/operator_reads.py`
- Modify: `backend/tests/integration/api/test_operator_read_repository.py`

**Interfaces:**
- Consumes: Task 1 storage schema from `evidence_records.structured_data` where `source_agent = 'TRACE'`.
- Produces: `parse_trace_waterfall(value: object) -> dict[str, Any] | None` and `OperatorReadService.get_trace_waterfall(identity, rca_run_id) -> {"trace": dict | None}`.

- [ ] **Step 1: Extend the integration fixture with valid Trace evidence**

Create a `TRACES` specialist run and insert one `evidence_records` row using the canonical five-Span structure. Use `AT` as `partition_timestamp` so the existing transaction-scoped partition is valid. Insert `raw_result` containing a unique secret marker and assert it never appears in the returned projection.

```python
waterfall = await repository.get_trace_waterfall(identity, RUN_ID)
assert waterfall["trace"]["trace_id"] == "trace-1"
assert waterfall["trace"]["spans"][3]["operation_name"] == "db.connection.acquire"
assert "raw_result" not in str(waterfall)
assert "secret-marker" not in str(waterfall)
```

Add tests for: no Trace row returns `{"trace": None}`; newer malformed Trace evidence is skipped in favor of the newest valid normalized row; a run outside the identity scope raises `OperatorResourceNotFound` exactly like a missing run.

- [ ] **Step 2: Run focused repository tests and verify failure**

```bash
cd backend
MIGRATION_TEST_DATABASE_URL='postgresql+asyncpg://postgres@127.0.0.1:55434/sre_agent' UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest tests/integration/api/test_operator_read_repository.py -k trace_waterfall -v
```

Expected: failure because `get_trace_waterfall` does not exist.

- [ ] **Step 3: Implement strict application projection models**

Create strict Pydantic models with snake_case fields and camelCase aliases. Enforce `schema_version == 1`, `0 <= start_offset_ms`, `0 <= duration_ms`, known status/kind literals, scalar attributes only, unique Span IDs, valid parents, and a maximum of 100 returned Spans.

```python
def parse_trace_waterfall(value: object) -> dict[str, Any] | None:
    try:
        model = StoredTraceWaterfall.model_validate(value)
    except ValidationError:
        return None
    if not model.has_valid_tree():
        return None
    return model.model_dump(by_alias=False)
```

This parser receives only `structured_data`; it has no parameter or code path for `raw_result`.

- [ ] **Step 4: Add the protocol and authorized repository query**

Add `get_trace_waterfall` to `OperatorReadService`, `UnavailableOperatorReadService`, and `SqlAlchemyOperatorReadRepository`. First authorize the run by joining its Incident with `_AUTHORIZED_INCIDENT`; then read Trace rows newest-first and return the first valid schema.

```sql
SELECT evidence.structured_data
FROM evidence_records evidence
JOIN rca_runs run ON run.id = evidence.rca_run_id
JOIN incidents incident ON incident.id = run.incident_id
WHERE run.id = :rca_run_id
  AND evidence.source_agent = 'TRACE'
  AND <existing _AUTHORIZED_INCIDENT predicate>
ORDER BY evidence.observed_at DESC, evidence.id DESC
LIMIT 20
```

Run existence/authorization as a separate query or preserve a boolean authorized-run result so an authorized run with no Trace returns `trace: null`, while missing/unauthorized runs raise `OperatorResourceNotFound`.

- [ ] **Step 5: Run the focused integration tests**

Run the Step 2 command. Expected: all Trace waterfall repository tests pass.

- [ ] **Step 6: Run Backend lint and type checks for the new projection**

```bash
cd backend
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check src/sre_agent/application/operator/trace_waterfall.py src/sre_agent/application/operator/read_models.py src/sre_agent/persistence/repositories/operator_reads.py tests/integration/api/test_operator_read_repository.py
UV_CACHE_DIR="$PWD/.uv-cache" uv run pyright src/sre_agent/application/operator/trace_waterfall.py src/sre_agent/application/operator/read_models.py src/sre_agent/persistence/repositories/operator_reads.py
```

Expected: both commands exit 0.

- [ ] **Step 7: Commit Task 3**

```bash
git add -- backend/src/sre_agent/application/operator/trace_waterfall.py backend/src/sre_agent/application/operator/read_models.py backend/src/sre_agent/persistence/repositories/operator_reads.py backend/tests/integration/api/test_operator_read_repository.py
git commit -m "feat(backend): project authorized trace waterfall"
```

---

### Task 4: Publish the Trace Waterfall HTTP Contract

**Files:**
- Modify: `backend/src/sre_agent/api/schemas/operator.py`
- Modify: `backend/src/sre_agent/api/routers/operator_incidents.py`
- Modify: `backend/tests/contract/api/test_operator_reads.py`
- Modify: `contracts/openapi/operator-api-v1.yaml`
- Create: `contracts/examples/trace-waterfall-response.json`
- Modify: `contracts/compatibility-tests/test_contracts.py`

**Interfaces:**
- Consumes: Task 3 `get_trace_waterfall` read-service method.
- Produces: `GET /api/v1/rca-runs/{id}/trace-waterfall -> TraceWaterfallResponse`.

- [ ] **Step 1: Add failing Backend route contract tests**

Extend `FakeReads` with a fixed five-Span response and record the requested run ID. Assert the path is registered, output is camelCase, attributes remain scalars, and `trace: null` serializes as JSON null.

```python
response = await client.get(f"/api/v1/rca-runs/{RUN_ID}/trace-waterfall")
assert response.status_code == 200
assert response.json()["trace"]["rootServiceName"] == "checkout-api"
assert response.json()["trace"]["spans"][3]["criticalPath"] is True
assert set(response.json()["trace"]["spans"][0]) == {
    "spanId", "parentSpanId", "serviceName", "operationName",
    "startOffsetMs", "durationMs", "status", "kind",
    "criticalPath", "attributes",
}
```

- [ ] **Step 2: Add failing OpenAPI compatibility assertions and example**

Add the path-to-schema mapping for `/api/v1/rca-runs/{id}/trace-waterfall` and load `contracts/examples/trace-waterfall-response.json` through the existing schema validator. The example contains the five approved `INC-227` Spans and no raw payload.

- [ ] **Step 3: Run contract tests and verify failure**

```bash
cd backend
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest tests/contract/api/test_operator_reads.py -k trace_waterfall -v
cd ../contracts
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest compatibility-tests/test_contracts.py -k trace_waterfall -v
```

Expected: Backend route and OpenAPI path assertions fail because they are not implemented.

- [ ] **Step 4: Implement strict FastAPI schemas and route**

Add `TraceWaterfallSpan`, `TraceWaterfall`, and `TraceWaterfallResponse` as `OperatorModel` subclasses. Use `Literal` for status/kind, scalar attribute values, and numeric bounds.

```python
class TraceWaterfallResponse(OperatorModel):
    trace: TraceWaterfall | None

@router.get(
    "/rca-runs/{id}/trace-waterfall",
    response_model=TraceWaterfallResponse,
)
async def get_trace_waterfall(
    id: UUID,
    service: Annotated[OperatorReadService, Depends(get_operator_read_service)],
    identity: Annotated[OperatorIdentity, Depends(_identity)],
) -> dict[str, object]:
    return await service.get_trace_waterfall(identity, id)
```

- [ ] **Step 5: Add matching OpenAPI schemas**

Define every required field, enum, bound, nullable parent, scalar attribute `additionalProperties`, and `additionalProperties: false` on the response, Trace, and Span objects. The response for a valid run with no Trace is `{"trace": null}`.

- [ ] **Step 6: Run Backend and compatibility contract tests**

Run the Step 3 commands. Expected: all selected tests pass.

- [ ] **Step 7: Run OpenAPI validation and lint**

```bash
cd contracts
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest compatibility-tests/test_contracts.py -v
cd ../backend
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check src/sre_agent/api/schemas/operator.py src/sre_agent/api/routers/operator_incidents.py tests/contract/api/test_operator_reads.py
```

Expected: all tests pass and Ruff exits 0.

- [ ] **Step 8: Commit Task 4**

```bash
git add -- backend/src/sre_agent/api/schemas/operator.py backend/src/sre_agent/api/routers/operator_incidents.py backend/tests/contract/api/test_operator_reads.py contracts/openapi/operator-api-v1.yaml contracts/examples/trace-waterfall-response.json contracts/compatibility-tests/test_contracts.py
git commit -m "feat(api): expose trace waterfall contract"
```

---

### Task 5: Add the Typed Frontend Trace Client

**Files:**
- Modify: `frontend/src/app/core/api/operator-api.models.ts`
- Modify: `frontend/src/app/core/api/operator-api.client.ts`
- Modify: `frontend/src/app/core/api/operator-api.client.spec.ts`

**Interfaces:**
- Consumes: Task 4 HTTP contract.
- Produces: `TraceWaterfallResponse`, `TraceWaterfall`, `TraceWaterfallSpan`, `TraceWaterfallLoadState`, and `OperatorApiClient.getTraceWaterfall(runId)`.

- [ ] **Step 1: Add a failing API client test**

```typescript
it('loads the encoded trace waterfall resource', () => {
  client.getTraceWaterfall('run/1').subscribe((value) =>
    expect(value.trace?.traceId).toBe('trace-1'),
  );
  const request = http.expectOne('/api/v1/rca-runs/run%2F1/trace-waterfall');
  request.flush({ trace: traceFixture });
});
```

The fixture must use the five-Span response shape from the contract example.

- [ ] **Step 2: Run the focused Frontend test and verify failure**

```bash
cd frontend
PATH='/Users/linyungyuan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:/opt/homebrew/opt/node@22/bin:/usr/bin:/bin' npm test -- --watch=false --include src/app/core/api/operator-api.client.spec.ts
```

Expected: TypeScript compilation fails because `getTraceWaterfall` and Trace models do not exist.

- [ ] **Step 3: Add exact TypeScript models and client method**

```typescript
export type TraceSpanStatus = 'OK' | 'ERROR' | 'UNSET';
export type TraceSpanKind = 'INTERNAL' | 'SERVER' | 'CLIENT' | 'PRODUCER' | 'CONSUMER';
export interface TraceWaterfallSpan {
  spanId: string;
  parentSpanId: string | null;
  serviceName: string;
  operationName: string;
  startOffsetMs: number;
  durationMs: number;
  status: TraceSpanStatus;
  kind: TraceSpanKind;
  criticalPath: boolean;
  attributes: Record<string, string | number | boolean>;
}
export interface TraceWaterfall {
  schemaVersion: 1;
  traceId: string;
  rootServiceName: string;
  rootOperationName: string;
  startedAt: string;
  durationMs: number;
  spanCount: number;
  representativeScore: number;
  truncated: boolean;
  spans: TraceWaterfallSpan[];
}
export interface TraceWaterfallResponse { trace: TraceWaterfall | null; }
export type TraceWaterfallLoadState =
  | { status: 'loading' }
  | { status: 'empty' }
  | { status: 'error' }
  | { status: 'ready'; trace: TraceWaterfall };
```

Add `getTraceWaterfall(runId: string): Observable<TraceWaterfallResponse>` using the existing private `get` helper and `encodeURIComponent`.

- [ ] **Step 4: Run the focused test**

Run the Step 2 command. Expected: all tests pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add -- frontend/src/app/core/api/operator-api.models.ts frontend/src/app/core/api/operator-api.client.ts frontend/src/app/core/api/operator-api.client.spec.ts
git commit -m "feat(frontend): add trace waterfall client"
```

---

### Task 6: Build the Accessible Waterfall Component

**Files:**
- Create: `frontend/src/app/features/rca/trace-waterfall.component.ts`
- Create: `frontend/src/app/features/rca/trace-waterfall.component.spec.ts`

**Interfaces:**
- Consumes: Task 5 `TraceWaterfall` and `TraceWaterfallLoadState`.
- Produces: standalone `TraceWaterfallComponent` with `state` input and `retry` output; it performs no HTTP calls.

- [ ] **Step 1: Write failing rendering and selection tests**

Cover these exact assertions:

```typescript
fixture.componentRef.setInput('state', { status: 'ready', trace: traceFixture });
fixture.detectChanges();
const rows = fixture.nativeElement.querySelectorAll('[data-span-id]');
expect(rows.length).toBe(5);
expect(rows[3].getAttribute('aria-selected')).toBe('true');
expect(rows[3].textContent).toContain('db.connection.acquire');
expect(fixture.nativeElement.textContent).toContain('1,480ms');
expect(fixture.nativeElement.textContent).toContain('Critical path');
```

Click `reserve-items` and assert the detail region updates. Dispatch Enter on a focused row and assert selection. Add independent tests for loading skeleton, empty message, error message/retry output, and truncated notice.

- [ ] **Step 2: Run the component test and verify failure**

```bash
cd frontend
PATH='/Users/linyungyuan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:/opt/homebrew/opt/node@22/bin:/usr/bin:/bin' npm test -- --watch=false --include src/app/features/rca/trace-waterfall.component.spec.ts
```

Expected: TypeScript compilation fails because the component does not exist.

- [ ] **Step 3: Implement pure tree and timeline calculations**

Use Angular signals/computed values. Convert the flat parent-first API list into view rows with depth. Clamp timeline style percentages so rounding cannot create negative or over-100 positions.

```typescript
readonly rows = computed(() => buildRows(this.state().trace));
readonly barStyle = (span: TraceWaterfallSpan, total: number) => ({
  left: `${Math.max(0, Math.min(100, (span.startOffsetMs / total) * 100))}%`,
  width: `${Math.max(0.5, Math.min(100, (span.durationMs / total) * 100))}%`,
});
```

Render each row as `button type="button" role="treeitem"`, set `aria-level`, `aria-selected`, and a visible focus outline. Use a horizontally scrollable timeline container with a stable minimum width. Pick the first `(status === 'ERROR' && criticalPath)` Span by default, falling back to root.

Implement deterministic service colors from a fixed accessible palette indexed by a stable string hash. Override error/critical-path bars with the approved red style.

- [ ] **Step 4: Run component tests**

Run the Step 2 command. Expected: all tests pass.

- [ ] **Step 5: Run Frontend build**

```bash
cd frontend
PATH='/Users/linyungyuan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:/opt/homebrew/opt/node@22/bin:/usr/bin:/bin' npm run build
```

Expected: Angular production build exits 0 with no template/type error.

- [ ] **Step 6: Commit Task 6**

```bash
git add -- frontend/src/app/features/rca/trace-waterfall.component.ts frontend/src/app/features/rca/trace-waterfall.component.spec.ts
git commit -m "feat(frontend): render trace waterfall"
```

---

### Task 7: Integrate Independent Trace Loading into the RCA Page

**Files:**
- Modify: `frontend/src/app/features/rca/rca-report.component.ts`
- Modify: `frontend/src/app/features/rca/rca-report.component.spec.ts`
- Modify: `frontend/src/app/features/incidents/incident-detail.component.ts`
- Create: `frontend/src/app/features/incidents/incident-detail.component.spec.ts`

**Interfaces:**
- Consumes: Task 5 API client and Task 6 component.
- Produces: report inputs `traceState` and `retryTrace`; Incident container `traceState$` with independent retry.

- [ ] **Step 1: Add failing RCA placement and retry tests**

Set a ready Trace state on `RcaReportComponent`; assert the waterfall heading appears after the Impact heading and before Recommendations in DOM order. Set an error state, click `重新載入 Trace`, and assert the `retryTrace` output emits once.

- [ ] **Step 2: Add a failing Incident isolation test**

Configure router and HTTP testing providers, navigate to a fixed Incident, return a valid Incident/run/report, then make `/trace-waterfall` return 503. Assert the RCA root cause remains visible while the Trace block shows its retry action. Click retry, flush a successful Trace response, and assert the five rows appear without re-requesting the Incident or report.

- [ ] **Step 3: Run the focused integration tests and verify failure**

```bash
cd frontend
PATH='/Users/linyungyuan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:/opt/homebrew/opt/node@22/bin:/usr/bin:/bin' npm test -- --watch=false --include src/app/features/rca/rca-report.component.spec.ts --include src/app/features/incidents/incident-detail.component.spec.ts
```

Expected: failures because RCA report has no waterfall inputs and Incident detail has no independent Trace state.

- [ ] **Step 4: Nest the component at the approved location**

In `RcaReportComponent`, import `TraceWaterfallComponent`, add `traceState` input and `retryTrace` output, and place the component after the Impact paragraph and before the Recommendations heading.

- [ ] **Step 5: Implement independent RxJS loading and retry**

Share the existing Incident/run/report context so Trace retries do not repeat it. Derive `traceState$` from the latest run plus a private retry subject.

```typescript
private readonly traceReload = new BehaviorSubject<void>(undefined);
readonly view$ = this.loadView().pipe(shareReplay({ bufferSize: 1, refCount: true }));
readonly traceState$ = combineLatest([this.view$, this.traceReload]).pipe(
  switchMap(([view]) =>
    view.run
      ? this.api.getTraceWaterfall(view.run.id).pipe(
          map(({ trace }) => trace
            ? ({ status: 'ready', trace } as const)
            : ({ status: 'empty' } as const)),
          startWith({ status: 'loading' } as const),
          catchError(() => of({ status: 'error' } as const)),
        )
      : of({ status: 'empty' } as const),
  ),
);
retryTrace(): void { this.traceReload.next(); }
```

Keep the main `view$` report path unchanged except for extracting it into `loadView()` and adding `shareReplay`. Do not put the Trace request inside the existing `forkJoin`.

- [ ] **Step 6: Run focused tests and full Frontend suite**

```bash
cd frontend
PATH='/Users/linyungyuan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:/opt/homebrew/opt/node@22/bin:/usr/bin:/bin' npm test -- --watch=false
PATH='/Users/linyungyuan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:/opt/homebrew/opt/node@22/bin:/usr/bin:/bin' npm run build
```

Expected: all Frontend tests pass and production build exits 0.

- [ ] **Step 7: Commit Task 7**

```bash
git add -- frontend/src/app/features/rca/rca-report.component.ts frontend/src/app/features/rca/rca-report.component.spec.ts frontend/src/app/features/incidents/incident-detail.component.ts frontend/src/app/features/incidents/incident-detail.component.spec.ts
git commit -m "feat(frontend): integrate RCA trace waterfall"
```

---

### Task 8: Seed the Local Demonstration and Verify End to End

**Files:**
- Modify no repository file for the local data update.
- Verify all files changed by Tasks 1–7.

**Interfaces:**
- Consumes: the shipped API/UI and local Incident `d0000000-0000-4000-8000-000000000001` / RCA run `d1000000-0000-4000-8000-000000000001`.
- Produces: one locally visible five-Span waterfall and fresh full-suite verification evidence.

- [ ] **Step 1: Update only the local Trace evidence JSON**

Update `evidence_records.structured_data` for evidence ID `d3000000-0000-4000-8000-000000000002` and partition timestamp `2026-08-23T04:35:00Z`. Use the exact body from `contracts/examples/trace-waterfall-response.json` under its `trace` key, with `schemaVersion: 1`, `representativeScore: 0.94`, five Spans, and `truncated: false`. Do not change `raw_result`.

Run from the repository root:

```bash
TRACE_WATERFALL_JSON="$(jq -c '.trace' contracts/examples/trace-waterfall-response.json)"
docker exec sre-agent20-local-postgres psql -U postgres -d sre_agent \
  -v ON_ERROR_STOP=1 \
  -v "trace_waterfall_json=$TRACE_WATERFALL_JSON" \
  -c "UPDATE evidence_records SET structured_data=:'trace_waterfall_json'::jsonb WHERE id='d3000000-0000-4000-8000-000000000002' AND partition_timestamp='2026-08-23T04:35:00Z'" \
  -c "SELECT structured_data->>'traceId' AS trace_id, jsonb_array_length(structured_data->'spans') AS span_count, octet_length(raw_result) AS raw_bytes FROM evidence_records WHERE id='d3000000-0000-4000-8000-000000000002' AND partition_timestamp='2026-08-23T04:35:00Z'"
```

Expected: `trace_id=4bf92f3577b34da6a3ce929d0e0e4736`, `span_count=5`, and `raw_bytes` remains greater than zero.

- [ ] **Step 2: Run complete Worker verification**

```bash
cd rca-worker
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest -v
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check .
UV_CACHE_DIR="$PWD/.uv-cache" uv run pyright src
```

Expected: zero test failures and all static checks exit 0.

- [ ] **Step 3: Run complete Backend verification**

```bash
cd backend
MIGRATION_TEST_DATABASE_URL='postgresql+asyncpg://postgres@127.0.0.1:55434/sre_agent' UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest -v
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check .
UV_CACHE_DIR="$PWD/.uv-cache" uv run pyright src
```

Expected: zero test failures and all static checks exit 0.

- [ ] **Step 4: Run complete contract verification**

```bash
cd contracts
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest compatibility-tests -v
```

Expected: zero test failures.

- [ ] **Step 5: Run complete Frontend verification**

```bash
cd frontend
PATH='/Users/linyungyuan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:/opt/homebrew/opt/node@22/bin:/usr/bin:/bin' npm test -- --watch=false
PATH='/Users/linyungyuan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:/opt/homebrew/opt/node@22/bin:/usr/bin:/bin' npm run build
```

Expected: zero test failures and Angular production build exits 0.

- [ ] **Step 6: Restart changed local application processes**

Restart Backend, RCA Worker, and Frontend using the documented local commands in `README.md`. Keep PostgreSQL, Pub/Sub Emulator, and Outbox Publisher running unless their health checks fail. Start Frontend with `--watch=false` in this desktop environment to avoid the known file-watcher limit.

- [ ] **Step 7: Verify the local API and UI**

```bash
curl -fsS 'http://127.0.0.1:4200/api/v1/rca-runs/d1000000-0000-4000-8000-000000000001/trace-waterfall'
curl -fsS 'http://127.0.0.1:4200/incidents/d0000000-0000-4000-8000-000000000001'
```

Expected: API returns one five-Span Trace with no raw fields; page returns HTTP 200. In the in-app browser, verify the red `db.connection.acquire` Span is selected initially, clicking `reserve-items` updates the detail panel, keyboard Enter changes selection, and the RCA report remains visible when a forced Trace request fails.

- [ ] **Step 8: Review the final diff and commit any verification-only documentation changes**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors and only intended files changed. Local database changes and `.superpowers/brainstorm` files must not appear in the commit.

If implementation required no documentation correction, create no extra commit. If an exact startup or verification command in `README.md` had to change, commit only that correction:

```bash
git add -- README.md
git commit -m "docs: document trace waterfall verification"
```
