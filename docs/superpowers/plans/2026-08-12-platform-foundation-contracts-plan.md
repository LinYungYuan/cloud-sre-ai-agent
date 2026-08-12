# Platform Foundation and Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create independently testable backend/frontend foundations plus canonical v1 HTTP contracts.

**Architecture:** The backend uses a Python `src/` package with domain types that do not depend on FastAPI or SQLAlchemy. The Angular project is independently buildable. OpenAPI and JSON Schema files under `contracts/` are the only shared integration boundary.

**Tech Stack:** Python 3.11+, uv, FastAPI, Pydantic v2, pytest, Ruff, Pyright, Angular 22 standalone components, Node.js 24.15+, TypeScript strict mode, OpenAPI 3.1, JSON Schema 2020-12.

## Global Constraints

- User-facing UI and AI narrative use Traditional Chinese (`zh-TW`).
- API/database identifiers and enum values remain English.
- API and database timestamps use UTC; Angular displays `Asia/Taipei`.
- Backend API, RCA worker, and Angular must build, test, version, and deploy independently.
- Angular major version is 22; use a Node.js version supported by Angular 22 (`>=24.15.0 <25` for this project).
- Do not create `infrastructure/`, Terraform, or Kubernetes files.
- Do not implement a production Identity Provider in this plan.
- Do not add write/remediation MCP tools.

---

## File map

- `backend/pyproject.toml`: Python dependencies and quality commands.
- `backend/src/sre_agent/config/settings.py`: typed runtime configuration.
- `backend/src/sre_agent/domain/common.py`: shared UUID/time/value types.
- `backend/src/sre_agent/domain/alerts/models.py`: canonical alert enums and values.
- `backend/src/sre_agent/domain/incidents/models.py`: Incident and RCA state enums.
- `contracts/openapi/grafana-webhook-v1.yaml`: public machine ingestion contract.
- `contracts/openapi/operator-api-v1.yaml`: Angular-facing contract.
- `contracts/examples/`: valid contract examples used by tests.
- `frontend/`: standalone Angular workspace, initially a shell only.
- `scripts/contract_check/check_contracts.py`: deterministic schema/OpenAPI validation.

### Task 1: Backend package and quality gate

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/src/sre_agent/__init__.py`
- Create: `backend/src/sre_agent/config/settings.py`
- Create: `backend/tests/unit/config/test_settings.py`
- Create: `backend/README.md`

**Interfaces:**
- Produces: `Settings(database_url, pubsub_project_id, rca_topic_id, app_environment, model_name, metrics_mcp_url, trace_mcp_url, log_mcp_url)`.

- [ ] **Step 1: Write the failing configuration test**

```python
from sre_agent.config.settings import Settings


def test_settings_reads_explicit_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app:test@db/sre")
    monkeypatch.setenv("PUBSUB_PROJECT_ID", "local-project")
    monkeypatch.setenv("RCA_TOPIC_ID", "rca-jobs")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    monkeypatch.setenv("METRICS_MCP_URL", "https://gateway/gcp/metrics/mcp")
    monkeypatch.setenv("TRACE_MCP_URL", "https://gateway/gcp/trace/mcp")
    monkeypatch.setenv("LOG_MCP_URL", "https://gateway/gcp/log/mcp")

    settings = Settings()

    assert settings.app_environment == "test"
    assert settings.rca_deadline_seconds == 300
```

- [ ] **Step 2: Add the backend package definition and run the failing test**

`backend/pyproject.toml` must declare Python `>=3.11`, runtime dependencies `fastapi`, `pydantic`, `pydantic-settings`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `httpx`, `google-cloud-pubsub`, `structlog`, `opentelemetry-api`, `pyyaml`, and development dependencies `pytest`, `pytest-asyncio`, `ruff`, `pyright`, `jsonschema`, `openapi-spec-validator`.

Run: `cd backend && uv sync && uv run pytest tests/unit/config/test_settings.py -v`
Expected: FAIL because `sre_agent.config.settings` does not exist.

- [ ] **Step 3: Implement strict settings**

```python
from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="forbid")

    database_url: str
    pubsub_project_id: str
    rca_topic_id: str
    app_environment: str
    model_name: str
    metrics_mcp_url: AnyHttpUrl
    trace_mcp_url: AnyHttpUrl
    log_mcp_url: AnyHttpUrl
    rca_deadline_seconds: int = Field(default=300, ge=60, le=300)
    webhook_max_body_bytes: int = Field(default=1_048_576, ge=1024)
```

- [ ] **Step 4: Run backend quality gates**

Run: `cd backend && uv run pytest -q && uv run ruff check . && uv run pyright`
Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "build: scaffold backend package"
```

### Task 2: Canonical domain states

**Files:**
- Create: `backend/src/sre_agent/domain/common.py`
- Create: `backend/src/sre_agent/domain/alerts/models.py`
- Create: `backend/src/sre_agent/domain/incidents/models.py`
- Create: `backend/tests/unit/domain/test_states.py`

**Interfaces:**
- Produces: `AlertState`, `ClassificationStatus`, `IncidentStatus`, `RcaRunStatus`, `EvidenceRelation`, and `UtcTimestamp`.
- Consumed by: every later backend plan and the OpenAPI enum definitions.

- [ ] **Step 1: Write state invariant tests**

```python
from sre_agent.domain.alerts.models import AlertState, ClassificationStatus
from sre_agent.domain.incidents.models import IncidentStatus, RcaRunStatus


def test_public_enum_values_are_stable():
    assert AlertState.FIRING.value == "FIRING"
    assert ClassificationStatus.UNCLASSIFIED.value == "UNCLASSIFIED"
    assert IncidentStatus.INVESTIGATING.value == "INVESTIGATING"
    assert RcaRunStatus.WAITING_FOR_CLASSIFICATION.value == "WAITING_FOR_CLASSIFICATION"
    assert RcaRunStatus.PARTIAL.value == "PARTIAL"
```

- [ ] **Step 2: Run the test and confirm missing imports**

Run: `cd backend && uv run pytest tests/unit/domain/test_states.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement string enums and UTC validation**

Use `enum.StrEnum` for the exact values in the test. Define `UtcTimestamp = Annotated[datetime, AfterValidator(require_aware_utc)]`; `require_aware_utc` rejects naive datetimes and normalizes aware values with `astimezone(timezone.utc)`.

- [ ] **Step 4: Add transition table tests and implementation**

Test that Incident permits `OPEN→INVESTIGATING→RESOLVED`, `RESOLVED→OPEN`, and rejects `OPEN→RESOLVED` unless the application use case supplies the explicit resolution command. Test RCA transitions from `WAITING_FOR_CLASSIFICATION→QUEUED`, `QUEUED→RUNNING`, and `RUNNING→SUCCEEDED|PARTIAL|FAILED|CANCELLED`.

- [ ] **Step 5: Run and commit**

Run: `cd backend && uv run pytest tests/unit/domain -v`
Expected: PASS.

```bash
git add backend/src/sre_agent/domain backend/tests/unit/domain
git commit -m "feat: define canonical alert and incident states"
```

### Task 3: Grafana webhook OpenAPI contract

**Files:**
- Create: `contracts/openapi/grafana-webhook-v1.yaml`
- Create: `contracts/examples/grafana-firing.json`
- Create: `contracts/examples/grafana-resolved.json`
- Create: `contracts/examples/webhook-accepted.json`
- Create: `scripts/contract_check/__init__.py`
- Create: `scripts/contract_check/check_contracts.py`
- Create: `contracts/compatibility-tests/test_contracts.py`

**Interfaces:**
- Produces: `POST /webhooks/v1/grafana/{sourceId}` and schemas `GrafanaWebhook`, `GrafanaAlert`, `WebhookAccepted`, `Problem`.

- [ ] **Step 1: Add a failing validator test**

```python
from pathlib import Path
from scripts.contract_check.check_contracts import validate_all


def test_all_contracts_and_examples_are_valid():
    validate_all(Path(__file__).parents[2])
```

- [ ] **Step 2: Run the test**

Run: `uv run --project backend pytest contracts/compatibility-tests/test_contracts.py -v`
Expected: FAIL because contracts do not exist.

- [ ] **Step 3: Write the Grafana contract**

Define OpenAPI 3.1 with `sourceId` path UUID/string, Bearer security, JSON body limit documented as 1 MiB, `202/400/401/413/500` responses, and these exact alert fields: `status`, `labels`, `annotations`, `startsAt`, `endsAt`, `values`, `generatorURL`, `fingerprint`, `silenceURL`, `dashboardURL`, `panelURL`, `imageURL`. Preserve unknown Grafana top-level and alert fields using `additionalProperties: true` so payload evolution is not destructive.

- [ ] **Step 4: Implement validation**

`validate_all(root)` must call `openapi_spec_validator.validate()` for each YAML contract and `jsonschema.Draft202012Validator.check_schema()` for each JSON Schema. It must load the firing/resolved examples and validate them against the referenced schemas using a resolver rooted at `contracts/`.

- [ ] **Step 5: Run and commit**

Run: `uv run --project backend pytest contracts/compatibility-tests/test_contracts.py -v`
Expected: PASS.

```bash
git add contracts scripts/contract_check
git commit -m "feat: define Grafana webhook v1 contract"
```

### Task 4: Operator REST API contract

**Files:**
- Create: `contracts/openapi/operator-api-v1.yaml`
- Create: `contracts/examples/incident.json`
- Create: `contracts/examples/rca-report.json`
- Modify: `scripts/contract_check/check_contracts.py`
- Modify: `contracts/compatibility-tests/test_contracts.py`

**Interfaces:**
- Produces all approved Operator REST `/api/v1` resources.
- Consumed by: FastAPI response models and the generated Angular client.

- [ ] **Step 1: Extend the contract test with required operations**

Assert that the parsed OpenAPI `paths` contains every endpoint listed in design section 10.2 and that every mutation declares either `Idempotency-Key` or `If-Match`. Assert list responses expose `items` and `nextCursor`, never `offset`.

- [ ] **Step 2: Run and observe the failure**

Run: `uv run --project backend pytest contracts/compatibility-tests/test_contracts.py -v`
Expected: FAIL because `operator-api-v1.yaml` is missing.

- [ ] **Step 3: Write exact API schemas**

Define `IncidentSummary`, `IncidentDetail`, `AlertSummary`, `RcaRun`, `RcaReport`, `Evidence`, `Hypothesis`, `IncidentMessage`, `TimelineEvent`, `CursorPage*`, and RFC 9457-style `Problem`. Use UUID strings, `date-time`, English enums from Task 2, ETag response headers for Incident resources, and stable error codes including `RCA_ALREADY_RUNNING`, `INCIDENT_VERSION_CONFLICT`, `SCOPE_FORBIDDEN`, `MCP_TIMEOUT`.

- [ ] **Step 4: Lock REST-only operator behavior and schema invariants**

Assert no public browser event-stream path or event contract exists. Include dashboard counts/trends/recent Incidents, current RCA status, scope/filter/sort parameters, mapping ETags, and Incident acknowledgement/resolution invariants. The UI obtains new state only through authenticated REST after a user refresh or explicit reload.

- [ ] **Step 5: Validate examples and commit**

Run: `uv run --project backend pytest contracts/compatibility-tests/test_contracts.py -v`
Expected: PASS.

```bash
git add contracts scripts/contract_check
git commit -m "feat: define operator REST API contract"
```

### Task 5: Independent Angular shell

**Files:**
- Create: `frontend/angular.json`
- Create: `frontend/package.json`
- Create: `frontend/package-lock.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/tsconfig.app.json`
- Create: `frontend/tsconfig.spec.json`
- Create: `frontend/src/main.ts`
- Create: `frontend/src/index.html`
- Create: `frontend/src/app/app.component.ts`
- Create: `frontend/src/app/app.routes.ts`
- Create: `frontend/src/app/core/runtime-config/runtime-config.ts`
- Create: `frontend/src/app/core/runtime-config/runtime-config.spec.ts`
- Create: `frontend/public/config.json`
- Create: `frontend/README.md`

**Interfaces:**
- Produces: `RuntimeConfig { apiBaseUrl: string; locale: 'zh-TW'; timeZone: 'Asia/Taipei' }`.

- [ ] **Step 1: Scaffold an Angular standalone app without Git or routing boilerplate outside `frontend/`**

Run from repository root with Node.js `>=24.15.0 <25`: `npx @angular/cli@22 new frontend --standalone --routing --style=scss --skip-git --strict --package-manager=npm`
Expected: only `frontend/` is created or modified.

- [ ] **Step 2: Write the failing runtime configuration test**

```typescript
it('rejects a locale other than zh-TW', () => {
  expect(() => parseRuntimeConfig({
    apiBaseUrl: '/api/v1',
    locale: 'en-US', timeZone: 'Asia/Taipei'
  })).toThrowError(/zh-TW/);
});
```

- [ ] **Step 3: Run the failing test**

Run: `cd frontend && npm test -- --watch=false`
Expected: FAIL because `parseRuntimeConfig` does not exist.

- [ ] **Step 4: Implement runtime configuration and zh-TW bootstrap**

Implement `parseRuntimeConfig(value: unknown): RuntimeConfig` with explicit string/property checks. Bootstrap using `LOCALE_ID = 'zh-TW'`, register `@angular/common/locales/zh-Hant`, and fetch `/config.json` before application startup. No API URL may be hard-coded in a feature component.

- [ ] **Step 5: Run independent frontend gates**

Run: `cd frontend && npm test -- --watch=false && npm run build`
Expected: PASS and production artifacts under `frontend/dist/`.

- [ ] **Step 6: Commit**

```bash
git add frontend
git commit -m "build: scaffold independent Angular shell"
```

### Task 6: Root developer commands and final foundation verification

**Files:**
- Create: `Makefile`
- Create: `README.md`
- Create: `.gitignore`

**Interfaces:**
- Produces: `make test-backend`, `make test-contracts`, `make test-frontend`, `make check`.

- [ ] **Step 1: Add exact root commands**

`make check` must run contract validation, backend pytest/Ruff/Pyright, then Angular tests/build. `.gitignore` covers `.venv`, `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.pyright`, `node_modules`, `dist`, `.angular`, `.env*`, while allowing `.env.example`.

- [ ] **Step 2: Document independent workflows**

README must show backend setup, frontend setup, contract validation, runtime configuration, and state explicitly that infrastructure provisioning is outside this repository.

- [ ] **Step 3: Run final verification**

Run: `make check`
Expected: all backend, contract, and frontend commands exit 0.

- [ ] **Step 4: Inspect repository boundary and commit**

Run: `test ! -d infrastructure && git diff --check`
Expected: exit 0.

```bash
git add .gitignore Makefile README.md
git commit -m "docs: add independent development workflows"
```
