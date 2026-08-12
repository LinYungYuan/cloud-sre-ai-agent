# Angular Operator UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver an independently deployable Traditional Chinese Angular interface for alert triage, shared investigation, evidence-backed RCA, and Incident operations.

**Architecture:** Lazy standalone feature routes consume a generated OpenAPI client through feature services. Signals hold local feature state. Authenticated REST is the only backend boundary; components never depend on backend source or database models. Users refresh the page or invoke an explicit reload action to obtain new server state.

**Tech Stack:** Angular 22 standalone components, Node.js 24.15+, TypeScript strict mode, Angular signals, Angular i18n/localization, generated OpenAPI client, SCSS, the Angular 22 scaffolded unit-test runner, Playwright for E2E.

## Global Constraints

- Every system label, action, state, hint, and error is Traditional Chinese (`zh-TW`).
- Technical identifiers and raw evidence remain unchanged.
- Dates display in `Asia/Taipei`; API values remain UTC.
- The frontend never connects to PostgreSQL or imports backend files.
- The API URL comes from runtime configuration.
- Do not implement browser event streams, background polling, or automatic data refresh.
- Backend remains the authorization boundary.
- All pages implement loading, empty, partial, error, and unauthorized states.
- Important state is not color-only; keyboard and basic accessibility are required.
- No infrastructure provisioning files.
- Use Angular 22 with Node.js `>=24.15.0 <25`; lock all npm dependencies in `package-lock.json`.

---

## File map

- `frontend/src/app/core/api-client/`: generated, never manually edited.
- `frontend/src/app/core/i18n/`: enum/error zh-TW mappings.
- `frontend/src/app/layout/`: shell and navigation.
- `frontend/src/app/features/`: dashboard, incidents, alerts, classification, mappings.
- `frontend/e2e/`: critical browser journeys.

### Task 1: Generate and protect the API client

**Files:**
- Modify: `frontend/package.json`
- Create: `frontend/openapitools.json`
- Generate: `frontend/src/app/core/api-client/**`
- Create: `frontend/src/app/core/api-client/client-generation.spec.ts`

**Interfaces:**
- Consumes: `contracts/openapi/operator-api-v1.yaml`.
- Produces typed Angular services/models only from the published contract.

- [ ] **Step 1: Add deterministic generation command**

Add `npm run api:generate` using a pinned OpenAPI Generator CLI package and `npm run api:check` that regenerates to a temporary directory and diffs it against committed generated output.

- [ ] **Step 2: Generate the client**

Run: `cd frontend && npm run api:generate`
Expected: typed models include `IncidentSummary`, `RcaReport`, `Evidence`, `Problem`; services expose approved `/api/v1` operations.

- [ ] **Step 3: Add a source-boundary lint/test**

Test or lint rule rejects imports containing `/backend/`, `sqlalchemy`, or server-internal paths. Generated code is not manually edited but remains included in TypeScript compilation and the generation-diff check.

- [ ] **Step 4: Verify and commit**

Run: `cd frontend && npm run api:check && npm test -- --watch=false && npm run build`
Expected: PASS.

```bash
git add frontend
git commit -m "feat: generate Angular operator API client"
```

### Task 2: zh-TW design shell and navigation

**Files:**
- Create: `frontend/src/app/layout/app-shell/app-shell.component.*`
- Create: `frontend/src/app/layout/navigation/navigation.component.*`
- Create: `frontend/src/app/core/i18n/status-labels.ts`
- Create: `frontend/src/app/core/i18n/problem-messages.ts`
- Create: `frontend/src/app/shared/components/page-state/page-state.component.*`
- Modify: `frontend/src/app/app.routes.ts`
- Create: corresponding `*.spec.ts` files

**Interfaces:**
- Produces zh-TW navigation and `statusLabel(enumValue)`, `problemMessage(code, correlationId)`.

- [ ] **Step 1: Write translation completeness tests**

Iterate every contract enum and approved error code. Assert a non-empty zh-TW label exists, unknown values display a safe fallback plus raw code, and technical identifiers are not translated.

- [ ] **Step 2: Run failing tests**

Run: `cd frontend && npm test -- --watch=false`
Expected: FAIL with missing mappings/components.

- [ ] **Step 3: Implement shell**

Navigation labels are `總覽`, `事件`, `告警`, `未分類`, `分類規則`, `設定`. Use semantic `nav/main`, visible focus styles, skip link, route-active text/icon state, and permission-aware hiding supplied by an auth facade (never treated as enforcement).

- [ ] **Step 4: Implement reusable page states**

Create explicit loading, empty, partial, error, unauthorized variants. Error variant displays localized message and correlation ID with a copy button.

- [ ] **Step 5: Verify and commit**

Run: `cd frontend && npm test -- --watch=false && npm run build`
Expected: PASS.

```bash
git add frontend/src/app/layout frontend/src/app/core/i18n frontend/src/app/shared frontend/src/app/app.routes.ts
git commit -m "feat: add Traditional Chinese operator shell"
```

### Task 3: Dashboard and Incident list

**Files:**
- Create: `frontend/src/app/features/dashboard/**`
- Create: `frontend/src/app/features/incidents/incident-list/**`
- Create: `frontend/src/app/shared/components/filter-bar/**`
- Create: `frontend/src/app/shared/components/cursor-paginator/**`

**Interfaces:**
- Consumes generated dashboard/Incident list methods.
- Produces `/dashboard` and `/incidents` lazy routes.

- [ ] **Step 1: Write component/service tests**

Test counts, severity/status accessible text, URL-backed filters, `指派給我`, cursor next/back history, error states, and an explicit reload action that preserves the visible table filters and sorting.

- [ ] **Step 2: Run failing tests**

Run: `cd frontend && npm test -- --watch=false`
Expected: FAIL.

- [ ] **Step 3: Implement dashboard**

Render active/critical/unacknowledged/unassigned, RCA status counts, unclassified count, recent Incidents, and 24-hour trend. Cards link to filtered lists. Provide text equivalents for chart values.

- [ ] **Step 4: Implement Incident table**

Columns: event ID, severity, title, service, environment, alert state, Incident status, RCA status, assignee, opened, updated. All sorting/filtering/pagination is server-side. Format dates with zh-TW and Asia/Taipei.

- [ ] **Step 5: Verify and commit**

Run: `cd frontend && npm test -- --watch=false && npm run build`
Expected: PASS.

```bash
git add frontend/src/app/features/dashboard frontend/src/app/features/incidents/incident-list frontend/src/app/shared
git commit -m "feat: add dashboard and incident list"
```

### Task 4: Incident detail operations and conflict handling

**Files:**
- Create: `frontend/src/app/features/incidents/incident-detail/**`
- Create: `frontend/src/app/features/incidents/incident-overview/**`
- Create: `frontend/src/app/features/incidents/incident-actions/**`

**Interfaces:**
- Consumes Incident detail and acknowledge/assign/resolve/reopen methods with ETags.

- [ ] **Step 1: Write operation tests**

Assert header/scope/status rendering, permitted action labels (`確認`, `指派`, `重新執行 RCA`, `結案`, `重新開啟`), confirmation dialogs, disabled pending state, successful ETag refresh, `INCIDENT_VERSION_CONFLICT` reload prompt, and forbidden state.

- [ ] **Step 2: Run failing tests**

Run: `cd frontend && npm test -- --watch=false`
Expected: FAIL.

- [ ] **Step 3: Implement detail shell and overview**

Create tabs `總覽`, `調查`, `RCA 報告`, `告警與證據`, `稽核紀錄`. Preserve selected tab in URL. External Grafana links use safe anchors and clear external-link labels.

- [ ] **Step 4: Implement mutations**

Send current ETag; on conflict retain unsent user input and offer `重新載入`. Never optimistically display a successful server state before mutation completes.

- [ ] **Step 5: Verify and commit**

Run: `cd frontend && npm test -- --watch=false && npm run build`
Expected: PASS.

```bash
git add frontend/src/app/features/incidents
git commit -m "feat: add incident detail and operations"
```

### Task 5: Shared investigation, RCA, evidence, and audit tabs

**Files:**
- Create: `frontend/src/app/features/investigation/shared-chat/**`
- Create: `frontend/src/app/features/investigation/incident-timeline/**`
- Create: `frontend/src/app/features/investigation/hypothesis-viewer/**`
- Create: `frontend/src/app/features/investigation/evidence-viewer/**`
- Create: `frontend/src/app/features/incidents/audit-log/**`

**Interfaces:**
- Consumes message, timeline, RCA report/history, evidence, hypothesis, and audit endpoints.

- [ ] **Step 1: Write evidence fidelity UI tests**

Assert every report claim renders evidence links, confidence includes explanatory text, supporting/contradicting/missing groups are distinct without color-only meaning, partial/failed cause is visible, raw JSON is lazy-loaded and permission-gated, and raw technical strings are unchanged.

- [ ] **Step 2: Write shared chat tests**

Assert ordered immutable messages, USER/AGENT/SYSTEM labels in zh-TW, queued sending state, retry-safe idempotency, RCA/evidence references on AI replies, keyboard submission, and no edit/delete controls.

- [ ] **Step 3: Run failing tests**

Run: `cd frontend && npm test -- --watch=false`
Expected: FAIL.

- [ ] **Step 4: Implement tabs and safe rendering**

Render AI text as plain text or sanitized Markdown using an allowlist; prohibit raw `innerHTML`. Virtualize or paginate long timeline/audit lists. Show specialist progress and `等待告警分類` for waiting RCA.

- [ ] **Step 5: Verify and commit**

Run: `cd frontend && npm test -- --watch=false && npm run build`
Expected: PASS.

```bash
git add frontend/src/app/features/investigation frontend/src/app/features/incidents/audit-log
git commit -m "feat: add shared investigation and evidence views"
```

### Task 6: Alerts, unclassified queue, and mapping management

**Files:**
- Create: `frontend/src/app/features/alerts/**`
- Create: `frontend/src/app/features/unclassified-alerts/**`
- Create: `frontend/src/app/features/mappings/**`

**Interfaces:**
- Consumes alert list/detail, classify, mapping CRUD, and mapping preview endpoints.

- [ ] **Step 1: Write feature tests**

Test alert lifecycle/source/rule/fingerprint/scope/labels/Incident/Grafana links, missing-field explanation, scope selector, batch selection, reusable mapping option, preview result, broad mapping warning, priority ordering, ETag conflict, and unauthorized state.

- [ ] **Step 2: Run failing tests**

Run: `cd frontend && npm test -- --watch=false`
Expected: FAIL.

- [ ] **Step 3: Implement Alerts and Unclassified pages**

Use server filters/cursors. Classification form requires team/project/environment/service, shows what was derived vs manually selected, and starts RCA progress display after successful classification.

- [ ] **Step 4: Implement mapping management**

Show priority, source, match conditions, target scope, enabled, last matched, creator. Preview before save; deletion UI explains it disables future matching and preserves history.

- [ ] **Step 5: Verify and commit**

Run: `cd frontend && npm test -- --watch=false && npm run build`
Expected: PASS.

```bash
git add frontend/src/app/features/alerts frontend/src/app/features/unclassified-alerts frontend/src/app/features/mappings
git commit -m "feat: add alert classification interface"
```

### Task 7: Settings and runtime information

**Files:**
- Create: `frontend/src/app/features/settings/settings.component.*`
- Create: `frontend/src/app/features/settings/settings.component.spec.ts`

**Interfaces:**
- Produces `/settings` without pretending the deferred Identity Provider can be configured yet.

- [ ] **Step 1: Write settings tests**

Assert the page shows frontend version, API reachability/status, locale `繁體中文（台灣）`, display timezone `Asia/Taipei`, and a clear message that identity configuration is managed outside this UI. Assert no secret, token, database URL, or MCP credential is rendered.

- [ ] **Step 2: Run failing tests**

Run: `cd frontend && npm test -- --watch=false`
Expected: FAIL because the feature does not exist.

- [ ] **Step 3: Implement read-only settings page**

Use runtime configuration plus the public health endpoint. Display identity integration as `由系統管理員設定` without claiming a concrete provider or authentication mode. Do not add editable identity, secret, database, Pub/Sub, or MCP configuration fields.

- [ ] **Step 4: Verify and commit**

Run: `cd frontend && npm test -- --watch=false && npm run build`
Expected: PASS.

```bash
git add frontend/src/app/features/settings
git commit -m "feat: add safe runtime settings view"
```

### Task 8: End-to-end and accessibility release gate

**Files:**
- Create: `frontend/playwright.config.ts`
- Create: `frontend/e2e/incident-lifecycle.spec.ts`
- Create: `frontend/e2e/scope-authorization.spec.ts`
- Modify: `frontend/package.json`

**Interfaces:**
- Produces `npm run e2e` and `npm run check` release gates.

- [ ] **Step 1: Implement deterministic REST fixtures**

Mock the published OpenAPI responses, not internal backend objects. Fixtures cover a new critical Incident, running specialists, partial then completed report, conversation, assignment, resolution, reopen, and unclassified workflow. State transitions become visible only after the test invokes the page's explicit reload action or performs browser refresh.

- [ ] **Step 2: Implement critical journey E2E**

Verify a user refreshes to see a new Incident, confirms/assigns it, explicitly reloads to observe progress, reads the evidence-backed zh-TW report, asks a shared question, resolves, reopens, and handles a version conflict. Verify another team's direct URL returns unauthorized without data flash.

- [ ] **Step 3: Add accessibility assertions**

Run automated accessibility checks on dashboard, Incident list/detail, dialog, and classification form; additionally assert keyboard tab order, focus restoration after dialogs, and non-color status text.

- [ ] **Step 4: Run final frontend gates**

Run: `cd frontend && npm run api:check && npm test -- --watch=false && npm run build && npm run e2e`
Expected: all commands PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend
git commit -m "test: verify Angular incident workflows"
```
