# Grafana Normalization and Operator UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept the approved standard Grafana body, derive provider solely from `resource.label.project_id`, create every Incident/RCA transactionally with identity v2, and display the resulting Alert/Incident/RCA data in an independent Traditional Chinese Angular UI.

**Architecture:** The backend owns webhook parsing, safe versioned normalization, PostgreSQL migrations, ingestion transactions, and Operator REST read models. The Angular project consumes only the versioned OpenAPI contract and runtime `apiBaseUrl`; it never imports backend code or database models. This plan must complete before the RCA Worker plan because it produces the canonical alert, Incident identity v2, evidence reference shape, and outbox payload that the worker consumes.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy async, Alembic, PostgreSQL 18, pytest, OpenAPI 3.1, Angular 22, TypeScript 6, RxJS 7, Vitest, npm 11.

## Global Constraints

- Webhook endpoint remains `POST /webhooks/v1/grafana/{sourceId}` with opaque Bearer authentication before body read.
- Raw body limit is exactly 1 MiB: 1,048,576 bytes accepted; 1,048,577 bytes rejected with `413`.
- Provider rule is exclusive: key `resource.label.project_id` present and non-blank means GCP; absent means AWS; present but invalid/blank means GCP plus `VALIDATION_FAILED`.
- `cloud_provider`, `DBInstanceIdentifier`, `Series`, ARN, resource type, folder, and text never override provider.
- `folder` is the project/system code; no team/environment/service labels are required.
- `AlertValues` remains exact untrusted text and is the primary alert issue passed to RCA.
- Severity mapping is case-insensitive: `ERROR -> SEV1`, `WARN|WARNING -> SEV3`, everything else `UNMAPPED` plus warning.
- Normal Incident identity is version 2 over `sourceId + folder + alertname`; invalid folder/alertname is isolated by fingerprint.
- `VALIDATION_FAILED` and `UNCLASSIFIED` alerts still create Incident, RCA run, `RCA_ANALYSIS` job, and `RCA_RUN_REQUESTED` outbox.
- `resolved` never resolves the human Incident.
- All ingestion artifacts are committed in one PostgreSQL transaction before `202`.
- Backend and frontend remain independently buildable, testable, versioned, and deployable.
- Angular UI and errors use Traditional Chinese; raw technical strings remain unchanged.
- No Chat, conversation jobs, SSE, WebSocket, `sre-chat-backend`, or production infrastructure provisioning.

---

## File Map

### Public contracts

- `contracts/examples/grafana-firing-aws.json`: replace with the approved Grafana AWS body.
- `contracts/examples/grafana-firing.json`: GCP variant containing `resource.label.project_id`.
- `contracts/openapi/grafana-webhook-v1.yaml`: standard Grafana v1 body and unknown-field preservation.
- `contracts/openapi/operator-api-v1.yaml`: nullable legacy scope plus provider/folder/issue/normalization fields.
- `contracts/compatibility-tests/test_contracts.py`: executable compatibility and example checks.

### Backend

- `backend/src/sre_agent/domain/alerts/provider.py`: exclusive provider decision.
- `backend/src/sre_agent/domain/alerts/severity.py`: exact severity mapping.
- `backend/src/sre_agent/domain/alerts/identity.py`: identity v2 canonical encoding.
- `backend/src/sre_agent/domain/alerts/normalization.py`: safe rule contracts and rule engine.
- `backend/src/sre_agent/integrations/grafana/payloads.py`: permissive raw labels with strict envelope validation.
- `backend/src/sre_agent/integrations/grafana/normalizer.py`: canonical event construction.
- `backend/migrations/versions/0002_grafana_normalization_v2.py`: forward-compatible schema changes.
- `backend/src/sre_agent/persistence/repositories/normalization.py`: enabled rule catalog.
- `backend/src/sre_agent/persistence/repositories/alerts.py`: canonical event persistence.
- `backend/src/sre_agent/persistence/repositories/incidents.py`: nullable legacy scope and identity version.
- `backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py`: mixed-alert transaction orchestration.
- `backend/src/sre_agent/api/routers/operator_incidents.py`: Incident/Alert/RCA read endpoints.
- `backend/src/sre_agent/application/operator/read_models.py`: typed Operator read service.

### Frontend

- `frontend/src/app/core/api/operator-api.models.ts`: OpenAPI-aligned TypeScript DTOs.
- `frontend/src/app/core/api/operator-api.client.ts`: REST client using runtime config.
- `frontend/src/app/features/incidents/`: Incident list/detail components.
- `frontend/src/app/features/alerts/`: Alert detail presentation.
- `frontend/src/app/features/rca/`: RCA report/evidence presentation.
- `frontend/src/app/shared/`: Traditional Chinese status/severity/warning formatters.

---

### Task 1: Replace the Grafana and Operator contracts

**Files:**
- Modify: `contracts/examples/grafana-firing-aws.json`
- Modify: `contracts/examples/grafana-firing.json`
- Modify: `contracts/openapi/grafana-webhook-v1.yaml`
- Modify: `contracts/openapi/operator-api-v1.yaml`
- Modify: `contracts/compatibility-tests/test_contracts.py`

**Interfaces:**
- Produces: `Provider = GCP | AWS`, `CanonicalSeverity = SEV1 | SEV3 | UNMAPPED`, nullable `Scope`, `AlertIssue`, `NormalizationInfo`, and `EvidenceReference(evidenceId, partitionTimestamp, relation)` schemas.
- Produces: examples used unchanged by backend contract tests and Angular fixtures.

- [ ] **Step 1: Write failing compatibility assertions**

Add tests that load both fixtures and assert the approved discriminator, body fields, and Operator shapes:

```python
def test_provider_examples_use_only_project_id_presence() -> None:
    aws = load_example("grafana-firing-aws.json")
    gcp = load_example("grafana-firing.json")
    assert "resource.label.project_id" not in aws["alerts"][0]["labels"]
    assert gcp["alerts"][0]["labels"]["resource.label.project_id"].strip()

def test_operator_alert_exposes_normalized_issue() -> None:
    schema = operator_schema("AlertDetail")
    assert {"provider", "folderCode", "alertName", "severityRaw",
            "severity", "issue", "normalizationWarnings"} <= set(schema["required"])
```

- [ ] **Step 2: Run the focused contract tests and confirm RED**

Run: `UV_CACHE_DIR=$PWD/.uv-cache uv run --project backend pytest contracts/compatibility-tests/test_contracts.py -v`

Expected: FAIL because the fixtures and Operator schemas still require legacy cross-cloud labels/scope.

- [ ] **Step 3: Update the checked-in Grafana examples**

Use the approved AWS body verbatim for `grafana-firing-aws.json`. Create the GCP fixture by adding a non-blank `resource.label.project_id` while retaining standard Grafana fields. Keep `additionalProperties: true` on Grafana envelope/alert/labels/annotations so extensions survive.

- [ ] **Step 4: Update the Operator OpenAPI schemas**

Make `Scope` properties nullable and add exact public fields:

```yaml
Provider:
  type: string
  enum: [GCP, AWS]
CanonicalSeverity:
  type: string
  enum: [SEV1, SEV3, UNMAPPED]
AlertIssue:
  type: object
  required: [rawText, source, contentType, untrusted]
  properties:
    rawText: {type: string}
    source: {const: grafana.annotations.AlertValues}
    contentType: {const: text/plain}
    untrusted: {const: true}
  additionalProperties: false
EvidenceReference:
  required: [evidenceId, partitionTimestamp, relation]
```

Remove the Incident message create/list operations from current release scope; do not add SSE paths.
Allow arbitrary JSON values in raw `labels` so a present non-string `resource.label.project_id` reaches per-alert validation and returns `202`/`VALIDATION_FAILED` instead of becoming an envelope-level `400`. Operator `AlertDetail.labels` must likewise preserve these raw JSON values.

- [ ] **Step 5: Validate examples and both OpenAPI documents**

Run: `UV_CACHE_DIR=$PWD/.uv-cache uv run --project backend pytest contracts/compatibility-tests -v`

Expected: all compatibility tests PASS.

- [ ] **Step 6: Commit the contract boundary**

```bash
git add contracts
git commit -m "feat: define standard Grafana normalization contract"
```

---

### Task 2: Implement provider, severity, issue, and identity v2 domain rules

**Files:**
- Create: `backend/src/sre_agent/domain/alerts/provider.py`
- Create: `backend/src/sre_agent/domain/alerts/severity.py`
- Create: `backend/src/sre_agent/domain/alerts/identity.py`
- Modify: `backend/src/sre_agent/domain/alerts/models.py`
- Modify: `backend/src/sre_agent/integrations/grafana/payloads.py`
- Test: `backend/tests/unit/domain/alerts/test_provider.py`
- Test: `backend/tests/unit/domain/alerts/test_severity.py`
- Test: `backend/tests/unit/domain/alerts/test_identity_v2.py`

**Interfaces:**
- Produces: `detect_provider(labels: Mapping[str, object]) -> ProviderDecision`.
- Produces: `map_severity(raw: object) -> SeverityDecision`.
- Produces: `make_incident_identity_v2(source_id: UUID, folder: str | None, alert_name: str | None, fingerprint: str) -> IncidentIdentity`.

- [ ] **Step 1: Write provider RED tests**

```python
@pytest.mark.parametrize(("labels", "provider", "valid"), [
    ({"resource.label.project_id": "p-123"}, Provider.GCP, True),
    ({}, Provider.AWS, True),
    ({"resource.label.project_id": "  "}, Provider.GCP, False),
    ({"resource.label.project_id": None}, Provider.GCP, False),
])
def test_provider_is_exclusively_key_presence(labels, provider, valid):
    result = detect_provider(labels)
    assert result.provider is provider
    assert (not result.errors) is valid
```

Also assert conflicting `cloud_provider`, ARN, `Series`, and DB fields never change the result.

- [ ] **Step 2: Run provider tests and confirm RED**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/domain/alerts/test_provider.py -v`

Expected: collection FAIL because `provider.py` does not exist.

- [ ] **Step 3: Implement exact provider types**

```python
class Provider(StrEnum):
    GCP = "GCP"
    AWS = "AWS"

@dataclass(frozen=True, slots=True)
class ProviderDecision:
    provider: Provider
    project_id: str | None
    errors: tuple[AlertValidationError, ...]
```

Presence must be tested with `PROJECT_KEY in labels`, not `labels.get(PROJECT_KEY)`.

Change `GrafanaAlert.labels` to `dict[str, JsonValue]`, where `JsonValue` recursively allows `str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]`; reject arrays/objects as a per-alert validation error during normalization rather than rejecting the whole webhook. Update fingerprint canonicalization to hash sorted canonical JSON labels without coercing their values to strings.

- [ ] **Step 4: Write and implement severity tests**

Test `ERROR/error -> SEV1`, `WARN/WARNING` in mixed case -> `SEV3`, and other/non-string values -> `UNMAPPED` with `severity_unmapped`. Implement:

```python
@dataclass(frozen=True, slots=True)
class SeverityDecision:
    raw: str | None
    canonical: Literal["SEV1", "SEV3", "UNMAPPED"]
    warnings: tuple[str, ...]
```

- [ ] **Step 5: Write identity collision and invalid fallback tests**

Assert the normal tuple is `(2, source_id, folder, alert_name)` and invalid values use `(2, source_id, "__invalid__", fingerprint)`. Prove `("a:b", "c")` differs from `("a", "b:c")`.

- [ ] **Step 6: Implement canonical length-prefixed identity**

```python
def _encode(parts: tuple[str, ...]) -> bytes:
    return b"".join(len(p.encode()).to_bytes(4, "big") + p.encode() for p in parts)
```

Return both `key=sha256(encoded).hexdigest()` and `version=2` plus validation errors.

- [ ] **Step 7: Run all Task 2 tests and static checks**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/domain/alerts/test_provider.py tests/unit/domain/alerts/test_severity.py tests/unit/domain/alerts/test_identity_v2.py -v`

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run ruff check src/sre_agent/domain/alerts tests/unit/domain/alerts`

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pyright src/sre_agent/domain/alerts`

Expected: all PASS, Pyright 0 errors.

- [ ] **Step 8: Commit**

```bash
git add backend/src/sre_agent/domain/alerts backend/tests/unit/domain/alerts
git commit -m "feat: derive Grafana provider and Incident identity"
```

---

### Task 3: Add safe versioned normalization rules

**Files:**
- Create: `backend/src/sre_agent/domain/alerts/normalization.py`
- Modify: `backend/src/sre_agent/integrations/grafana/normalizer.py`
- Test: `backend/tests/unit/domain/alerts/test_normalization_rules.py`
- Test: `backend/tests/unit/integrations/grafana/test_normalizer.py`

**Interfaces:**
- Consumes: `ProviderDecision`, `SeverityDecision` from Task 2.
- Produces: `NormalizationRule`, `NormalizationResult`, `SafeRuleEngine.normalize(alert: CanonicalBaseAlert, provider: ProviderDecision) -> NormalizationResult`.
- Produces: expanded `CanonicalAlertEvent` containing provider, folder, alert name, severity, issue, resource, rule reference, status, and warnings.

- [ ] **Step 1: Write strict rule schema RED tests**

Test conditions `exists`, `equals`, `prefix`, and constrained `format`; reject unknown operators, executable strings, SQL/script fields, empty output, and ambiguous equal-priority matches.

```python
def test_equal_priority_matches_are_unclassified():
    result = SafeRuleEngine((rule_a, rule_b)).normalize(alert)
    assert result.status is NormalizationStatus.UNCLASSIFIED
    assert result.warnings == ("normalization_rule_conflict",)
```

- [ ] **Step 2: Run and confirm RED**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/domain/alerts/test_normalization_rules.py -v`

Expected: collection FAIL for missing module.

- [ ] **Step 3: Implement immutable rule contracts**

Define Pydantic models with `extra="forbid"`:

```python
class RuleCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    operator: Literal["exists", "equals", "prefix", "format"]
    value: str | None = None

class RuleOutput(BaseModel):
    provider: Provider
    resource_type: str
    scope_path: str | None = None
    resource_id_path: str | None = None
    resource_name_path: str | None = None
    display_unit: str | None = None
```

The engine must reject `RuleOutput.provider != detected provider` rather than overwrite provider.

- [ ] **Step 4: Expand `CanonicalAlertEvent`**

Add immutable fields:

```python
provider: Provider
folder_code: str | None
alert_name: str | None
severity: SeverityDecision
issue: AlertIssue
resource: NormalizedResource | None
normalization_status: NormalizationStatus
normalization_rule_id: UUID | None
normalization_rule_version: int | None
normalization_warnings: tuple[str, ...]
```

`AlertIssue.raw_text` uses exact annotation text and `untrusted=True`; never parse fixed Account/DB/Value fields.

- [ ] **Step 5: Test unknown fields and multiple alerts**

Assert each `alerts[]` item receives an independent provider/result, nested values remain deep-frozen, and arbitrary AlertValues/extension fields remain unchanged.

- [ ] **Step 6: Run focused tests and commit**

Run: `cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/domain/alerts/test_normalization_rules.py tests/unit/integrations/grafana/test_normalizer.py -v`

Expected: PASS.

```bash
git add backend/src/sre_agent/domain/alerts/normalization.py backend/src/sre_agent/integrations/grafana/normalizer.py backend/tests/unit
git commit -m "feat: normalize Grafana alerts with safe versioned rules"
```

---

### Task 4: Add the forward-only normalization and identity v2 migration

**Files:**
- Create: `backend/migrations/versions/0002_grafana_normalization_v2.py`
- Modify: `backend/tests/integration/persistence/test_schema.py`
- Modify: `backend/tests/unit/persistence/test_schema_documentation.py`
- Modify: `docs/database/postgresql-schema.md`

**Interfaces:**
- Consumes: existing revision `0001_alert_incident_schema` without editing it.
- Produces: `normalization_rules`, `folder_scope_mappings`, canonical alert columns, nullable legacy Incident scope, and identity v2 constraints/indexes.

- [ ] **Step 1: Write schema catalog RED tests**

Assert:

```python
assert column("alert_events", "provider").type == "text"
assert column("incidents", "identity_version").default == "2"
assert column("incidents", "team_id").nullable
assert unique("normalization_rules", "source_id", "name", "version")
assert check("incidents", "identity_version IN (1, 2)")
```

Also assert evidence reference APIs can return both UUID and partition timestamp; do not add chat tables.

- [ ] **Step 2: Run fresh PostgreSQL 18 RED**

Run: `cd backend && POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic upgrade head && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run pytest tests/integration/persistence/test_schema.py -v`

Expected: FAIL because v2 objects are absent.

- [ ] **Step 3: Implement revision `0002`**

Use `op.add_column`/`op.create_table` and named constraints. Add `truncated_alerts INTEGER NOT NULL DEFAULT 0 CHECK >= 0` and `incomplete BOOLEAN NOT NULL DEFAULT false` to `webhook_deliveries`. Add to `alert_events`: provider, folder_code, alert_name, severity_raw, severity_canonical, issue JSONB, resource JSONB, normalization status/rule/version/warnings. Add to `incidents`: identity_version, provider, folder_code, alert_name; make legacy scope FKs nullable; expand severity check to include `UNMAPPED`.

Create:

```sql
CREATE TABLE normalization_rules (
  id UUID PRIMARY KEY,
  source_id UUID NULL REFERENCES grafana_sources(id),
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  priority INTEGER NOT NULL,
  provider TEXT NOT NULL CHECK (provider IN ('GCP','AWS')),
  conditions JSONB NOT NULL,
  output JSONB NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT true,
  created_by UUID NULL REFERENCES subjects(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE NULLS NOT DISTINCT (source_id, name, version),
  CHECK (version > 0)
);
CREATE TABLE folder_scope_mappings (
  id UUID PRIMARY KEY,
  source_id UUID NOT NULL REFERENCES grafana_sources(id),
  folder_code TEXT NOT NULL,
  team_id UUID NULL REFERENCES teams(id),
  project_id UUID NULL REFERENCES projects(id),
  environment_id UUID NULL REFERENCES environments(id),
  service_id UUID NULL REFERENCES services(id),
  enabled BOOLEAN NOT NULL DEFAULT true,
  created_by UUID NULL REFERENCES subjects(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_id, folder_code)
);
```

Reuse the existing adjacent hierarchy composite FKs/checks on the nullable mapping scope, and require at least one scope ID. Do not treat this mapping as provider or Incident identity input.

Preserve existing rows as identity version 1. New repository writes version 2 explicitly. Downgrade must document that v2 canonical columns/rules are lost.

- [ ] **Step 4: Run downgrade/upgrade and catalog GREEN**

Run: `cd backend && POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic downgrade 0001_alert_incident_schema && POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic upgrade head`

Run the schema test from Step 2 again. Expected: PASS.

- [ ] **Step 5: Update the canonical schema reference and parser guard**

Document every new column, FK, CHECK, UNIQUE, index, nullable legacy scope, downgrade warning, and the fact that `folder_code` is not `projects.id`. Run:

`cd backend && UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests/unit/persistence/test_schema_documentation.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/migrations/versions/0002_grafana_normalization_v2.py backend/tests/integration/persistence/test_schema.py backend/tests/unit/persistence/test_schema_documentation.py docs/database/postgresql-schema.md
git commit -m "feat: add Grafana normalization schema v2"
```

---

### Task 5: Load normalization rules and persist canonical alerts

**Files:**
- Create: `backend/src/sre_agent/persistence/repositories/normalization.py`
- Modify: `backend/src/sre_agent/api/composition.py`
- Modify: `backend/src/sre_agent/persistence/repositories/alerts.py`
- Modify: `backend/src/sre_agent/persistence/repositories/incidents.py`
- Modify: `backend/src/sre_agent/persistence/unit_of_work.py`
- Test: `backend/tests/integration/persistence/test_normalization_repository.py`

**Interfaces:**
- Produces: `NormalizationRuleProvider.for_source(source_id: UUID) -> SafeRuleEngine`.
- Produces: `FolderScopeProvider.resolve(source_id: UUID, folder_code: str | None) -> IncidentScope`.
- Produces: `IncidentScope(team_id: UUID | None, project_id: UUID | None, environment_id: UUID | None, service_id: UUID | None)`.
- Consumes: expanded `CanonicalAlertEvent` and identity version 2.

- [ ] **Step 1: Write repository RED tests**

Insert enabled/disabled/source-specific/global rules. Assert enabled candidates are ordered `(priority, created_at, id)`, exact version/output is reconstructed, invalid persisted JSON fails application startup, and no rule still returns an empty engine.

- [ ] **Step 2: Run and confirm RED**

Run: `cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run pytest tests/integration/persistence/test_normalization_repository.py -v`

Expected: collection FAIL for missing repository.

- [ ] **Step 3: Implement loader and composition**

Load rules at startup into an immutable source-aware provider. Load enabled `folder_scope_mappings` into an immutable `FolderScopeProvider`; a missing folder mapping returns an all-null `IncidentScope`. Do not query either catalog per alert. Extend `RuntimeResources` with both providers; fail startup on invalid rule schema, contradictory hierarchy mapping, or configured source mismatch.

Remove `classifier_provider`/`LoadedClassifierProvider` from ingestion composition. Leave legacy `classification_mappings` rows readable for historical administration, but do not consult them when normalizing or deciding whether to create a new Incident/RCA.

- [ ] **Step 4: Persist all canonical fields**

Extend `AlertRepository.add_event` parameters only through `CanonicalAlertEvent`; serialize issue/resource/warnings as JSONB. Update Incident insert to write `identity_version=2`, provider/folder/alert name and nullable legacy scope.

- [ ] **Step 5: Run repository and composition tests**

Run: `cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run pytest tests/integration/persistence/test_normalization_repository.py tests/contract/api/test_app_composition.py tests/integration/api/test_production_app_composition.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/sre_agent/persistence backend/src/sre_agent/api/composition.py backend/tests
git commit -m "feat: persist normalized Grafana alerts"
```

---

### Task 6: Rewrite ingestion around identity v2 and always-on RCA creation

**Files:**
- Modify: `backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py`
- Modify: `backend/src/sre_agent/domain/alerts/fingerprint.py`
- Modify: `backend/tests/integration/application/test_ingest_grafana_alerts.py`
- Modify: `backend/tests/contract/api/test_grafana_webhook.py`
- Modify: `backend/tests/unit/domain/alerts/test_fingerprint.py`

**Interfaces:**
- Consumes: Task 2 identities, Task 3 normalization provider, Task 5 repositories.
- Produces: one atomic result per delivery with valid, UNCLASSIFIED, and VALIDATION_FAILED alert artifacts.

- [ ] **Step 1: Add approved-body and provider RED cases**

Test the checked-in AWS fixture creates provider AWS, Incident, RCA/job/outbox and exact AlertValues. Add GCP valid and GCP blank-project cases. Blank GCP must persist `VALIDATION_FAILED` yet still create all work artifacts and no safe MCP scope.

- [ ] **Step 2: Add identity, mixed, and concurrency RED cases**

Test:

- same source/folder/alertname reuses active Incident even with different fingerprints;
- different source, folder, or alertname creates a different Incident;
- invalid folder/name uses fingerprint-isolated fallback;
- mixed valid/invalid webhook persists all events and marks delivery `VALIDATION_FAILED`;
- concurrent independent sessions create one active Incident/RCA/job/outbox;
- any exception after delivery insert rolls back all rows.
- lifecycle dedup uses exactly source ID, fingerprint, status, normalized startsAt, and normalized endsAt; changing unrelated raw whitespace does not create a second transition.
- `truncatedAlerts > 0` accepts received alerts, marks delivery incomplete, persists a warning, and increments the injected ingestion metric without inventing missing events.

- [ ] **Step 3: Run the focused suite and confirm RED**

Run: `cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run pytest tests/integration/application/test_ingest_grafana_alerts.py tests/contract/api/test_grafana_webhook.py -v`

Expected: failures showing old required labels and the old `continue` on validation errors.

- [ ] **Step 4: Implement the transaction flow**

Replace `CrossCloudAlertValidator`/legacy classifier gating with:

```python
decision = detect_provider(event.labels)
normalized = normalizer.for_source(source_id).normalize(event, decision)
identity = make_incident_identity_v2(
    source_id,
    normalized.canonical_event.folder_code,
    normalized.canonical_event.alert_name,
    event.fingerprint,
)
scope = folder_scopes.resolve(
    source_id, normalized.canonical_event.folder_code
)
stored = await uow.alerts.add_event(
    delivery_id=delivery_id,
    received_at=accepted_at,
    event=normalized.canonical_event,
    raw_payload=raw_alert,
    validation_status=normalized.validation_status.value,
    validation_errors=normalized.validation_errors,
)
await uow.alerts.upsert_instance(
    event=normalized.canonical_event,
    stored_event=stored,
    received_at=accepted_at,
)
selection = await uow.incidents.get_or_create_active(
    identity=identity,
    scope=scope,
    title=normalized.canonical_event.alert_name or event.fingerprint,
    severity=normalized.canonical_event.severity.canonical,
    opened_at=accepted_at,
    reopened_from_incident_id=reopened_from,
)
await uow.jobs.create_rca_work(
    incident_id=selection.id,
    run_status="QUEUED",
    available_at=accepted_at,
)
```

Never skip Incident/RCA creation because normalization is incomplete. Call `create_rca_work` only for newly created Incident; repository idempotency remains the second safety layer.

Update `make_dedup_key` to remove raw-body SHA from the lifecycle identity. Delivery dedup continues to use the exact raw-body SHA separately.

Persist `truncated_alerts`/`incomplete` on the delivery and call an injected `IngestionMetrics.record_truncated(source_id, count)` only after commit; metric failure must not roll back accepted data.

- [ ] **Step 5: Preserve HTTP semantics and latency boundary**

Ensure DB-unavailable errors map to `503`, generic failures to safe `500`, and `202` is returned only after UoW commit. Keep auth-before-stream and bounded read tests. Measure a real committed transaction under the 2-second boundary.

- [ ] **Step 6: Run focused and full backend suites**

Run the focused command from Step 3, then:

`cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests -v`

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/src/sre_agent/application/alerts/ingest_grafana_alerts.py backend/tests
git commit -m "feat: ingest standard Grafana alerts transactionally"
```

---

### Task 7: Implement Operator read endpoints for independent frontend use

**Files:**
- Create: `backend/src/sre_agent/application/operator/read_models.py`
- Create: `backend/src/sre_agent/persistence/repositories/operator_reads.py`
- Create: `backend/src/sre_agent/api/routers/operator_incidents.py`
- Create: `backend/src/sre_agent/api/schemas/operator.py`
- Modify: `backend/src/sre_agent/api/composition.py`
- Modify: `backend/src/sre_agent/api/dependencies.py`
- Modify: `backend/src/sre_agent/api/main.py`
- Test: `backend/tests/contract/api/test_operator_reads.py`
- Test: `backend/tests/integration/api/test_operator_reads.py`

**Interfaces:**
- Produces: `GET /api/v1/incidents`, `/api/v1/incidents/{id}`, `/api/v1/alerts/{id}`, `/api/v1/incidents/{id}/rca-runs`, `/api/v1/rca-runs/{id}/report` matching OpenAPI.
- Produces: cursor-based reads only; no Chat/message endpoints.

- [ ] **Step 1: Write contract RED tests from OpenAPI examples**

Assert exact camelCase response keys, UTC `Z`, nullable scope, provider, folder, AlertValues issue, warnings, ETag, correlation ID, RFC 9457 `404`, and bounded cursor pagination.

- [ ] **Step 2: Run and confirm RED**

Run: `cd backend && uv run pytest tests/contract/api/test_operator_reads.py -v`

Expected: `404` because no Operator router exists.

- [ ] **Step 3: Implement read-only repository and schemas**

Use Pydantic `extra="forbid"`; SQL must select explicit columns and always enforce `LIMIT <= 100`. Return evidence references with both UUID and partition timestamp. Do not return immutable webhook raw bytes from Operator endpoints.

- [ ] **Step 4: Wire authentication as a fail-closed boundary**

Add an injected `OperatorIdentityProvider` protocol. Production without a configured identity provider returns `503`/deny-by-default; test/local mock identity requires explicit `app_environment=local`. Repository filters by resolved folder scope or global-SRE grant.

- [ ] **Step 5: Run contract and PostgreSQL integration tests**

Run: `cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run pytest tests/contract/api/test_operator_reads.py tests/integration/api/test_operator_reads.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/sre_agent/application/operator backend/src/sre_agent/persistence/repositories/operator_reads.py backend/src/sre_agent/api backend/tests/contract/api/test_operator_reads.py backend/tests/integration/api/test_operator_reads.py
git commit -m "feat: expose normalized Incident read APIs"
```

---

### Task 8: Build the independent Angular Incident/Alert/RCA UI

**Files:**
- Create: `frontend/src/app/core/api/operator-api.models.ts`
- Create: `frontend/src/app/core/api/operator-api.client.ts`
- Create: `frontend/src/app/shared/presentation/incident-labels.ts`
- Create: `frontend/src/app/features/incidents/incident-list.component.ts`
- Create: `frontend/src/app/features/incidents/incident-detail.component.ts`
- Create: `frontend/src/app/features/alerts/alert-detail.component.ts`
- Create: `frontend/src/app/features/rca/rca-report.component.ts`
- Modify: `frontend/src/app/app.routes.ts`
- Modify: `frontend/src/app/app.config.ts`
- Test: matching `*.spec.ts` files beside each component/client.

**Interfaces:**
- Consumes: only Task 1 Operator OpenAPI and `RuntimeConfig.apiBaseUrl`.
- Produces: zh-TW list/detail views with manual refresh; no long-lived connection.

- [ ] **Step 1: Write API client RED tests**

Use a fake `HttpClient` and assert URL joins, cursor query encoding, RFC 9457 error mapping, and no backend imports. Define DTOs matching OpenAPI exactly:

```ts
export type Provider = 'GCP' | 'AWS';
export type CanonicalSeverity = 'SEV1' | 'SEV3' | 'UNMAPPED';
export interface AlertIssue {
  rawText: string;
  source: 'grafana.annotations.AlertValues';
  contentType: 'text/plain';
  untrusted: true;
}
```

- [ ] **Step 2: Run and confirm RED**

Run: `cd frontend && npm test -- --run`

Expected: FAIL because API client/components do not exist.

- [ ] **Step 3: Implement API client and presentation mapping**

Inject `RUNTIME_CONFIG`; use Angular `HttpClient`. Map statuses to fixed zh-TW strings, keep AlertValues/raw labels unchanged, and display `UNMAPPED` as `嚴重度未映射`.

Add `provideHttpClient()` to `app.config.ts`; do not create a second bootstrap path or read runtime config outside the existing startup coordinator.

- [ ] **Step 4: Implement Incident list and detail**

List shows Incident number, provider, folder, alert name, severity, state, RCA state, updated time, and a visible `重新整理` button. Detail loads Incident, linked alert, runs, and selected report via REST. Do not add timers, SSE, WebSocket, chat input, or message routes.

- [ ] **Step 5: Implement warning, issue, and report panels**

Alert panel labels AlertValues as `Grafana 告警內容`; warning panel shows `GCP project ID 空白`, `資源未分類`, `規則衝突`, or unmapped severity. PARTIAL report without MCP scope shows the approved Traditional Chinese explanation.

- [ ] **Step 6: Run Angular tests and bounded build**

Run: `cd frontend && npm test -- --run`

Run: `cd frontend && CI=1 NG_BUILD_MAX_WORKERS=1 npm run build`

Expected: all tests PASS; production build exits 0.

- [ ] **Step 7: Commit frontend independently**

```bash
git add frontend
git commit -m "feat: display normalized Incidents in Angular"
```

---

### Task 9: Cross-project acceptance, performance, and documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/database/postgresql-schema.md`
- Modify: `frontend/README.md`
- Create: `backend/tests/integration/api/test_grafana_to_operator_flow.py`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: one documented local flow from approved webhook to REST/UI data, without Chat.

- [ ] **Step 1: Add end-to-end RED test**

Post the approved AWS fixture to the real FastAPI composition backed by PostgreSQL 18, then query Operator Incident/Alert/RCA endpoints. Assert exact AlertValues, AWS provider, folder, SEV1, one Incident/RCA/job/outbox, and transaction completion under two seconds.

- [ ] **Step 2: Run RED then implement only missing composition/docs**

Run: `cd backend && MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run pytest tests/integration/api/test_grafana_to_operator_flow.py -v`

Expected before final wiring: FAIL at the first missing composition/read field. Add only the missing wiring; do not change domain semantics.

- [ ] **Step 3: Run fresh migration and all gates**

```bash
cd backend
POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic downgrade base
POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent uv run alembic upgrade head
MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/sre_agent UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests -v
UV_CACHE_DIR=$PWD/.uv-cache uv run ruff check .
UV_CACHE_DIR=$PWD/.uv-cache uv run pyright
```

Run: `UV_CACHE_DIR=$PWD/.uv-cache uv run --project backend pytest contracts/compatibility-tests -v`

Run: `cd frontend && npm test -- --run && CI=1 NG_BUILD_MAX_WORKERS=1 npm run build`

Expected: every command exits 0.

- [ ] **Step 4: Update run instructions and constraints**

Document exact env names, migration commands, approved provider rule, manual frontend refresh, identity v2, validation-failed RCA behavior, and the explicit absence of Chat/SSE. Do not add `infrastructure/`.

- [ ] **Step 5: Verify diff and commit**

Run: `git diff --check && test ! -d infrastructure`

```bash
git add README.md frontend/README.md docs/database/postgresql-schema.md backend/tests/integration/api/test_grafana_to_operator_flow.py
git commit -m "docs: document normalized Grafana Incident flow"
```
