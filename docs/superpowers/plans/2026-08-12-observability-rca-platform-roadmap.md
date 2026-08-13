# Observability RCA Multi-Agent Platform Implementation Roadmap

> **For agentic workers:** Execute the linked plans in order. Each plan is independently reviewable and ends in working, tested software.

**Goal:** Deliver the approved SRE AI Agent platform without coupling Angular, the operator API, Grafana ingestion, or the RCA runtime.

**Architecture:** A monorepo contains three independent packages: `backend/` FastAPI API, `rca-worker/` Python RCA runtime, and `frontend/` Angular SPA. Each has its own dependencies, lock, tests, Dockerfile, image, CI/build, and release; packages do not import each other's source. Versioned contracts are the cross-package boundary; Cloud SQL PostgreSQL 18 is the source of truth and Pub/Sub carries durable jobs through a transactional outbox. The browser uses authenticated REST with user-initiated refresh only.

**Approved design:** `docs/superpowers/specs/2026-08-12-observability-rca-platform-design.md`

## Execution order

1. `2026-08-12-platform-foundation-contracts-plan.md`
   - Create repository scaffolding, backend quality gates, canonical domain types, and versioned API/event contracts.
   - Exit condition: schemas validate and both projects can be built/tested independently.
2. `2026-08-12-grafana-ingestion-database-plan.md`
   - Create the PostgreSQL 18 schema, Grafana authentication/normalization, deduplication, classification, Incident lifecycle, outbox, and ingestion endpoint.
   - Exit condition: a real Grafana fixture produces exactly one durable Incident and one RCA job under redelivery.
3. `2026-08-13-grafana-normalization-operator-ui-plan.md`
   - Replace the old Grafana cross-cloud label contract, add identity v2/core migrations, Operator reads, and the independent Angular Incident/Alert/RCA UI.
   - Exit condition: the approved standard Grafana body is accepted, queryable, and visible after manual refresh.
4. `2026-08-13-pubsub-emulator-rca-worker-plan.md`
   - Create the worker, MCP capability boundaries, specialist execution, evidence/provenance persistence, hypothesis synthesis, deadlines, retries, and partial reports.
   - Exit condition: the independent `rca-worker/` package passes official Pub/Sub Emulator redelivery and produces an evidence-backed zh-TW report.
5. `2026-08-12-release-hardening-plan.md`
   - Add health/readiness, structured redacted telemetry, partition maintenance, admin bootstrap commands, independent containers, and end-to-end SLO verification.
   - Exit condition: API, worker, and Angular images pass independent smoke tests and the three timing objectives are measured.

## Cross-plan release gates

- Never add an `infrastructure/` directory, Terraform, or Kubernetes manifests.
- `backend/`, `rca-worker/`, and `frontend/` have separate dependency manifests, locks, build/test commands, Dockerfiles, images, and releases.
- Backend and Worker use separate runtime/migration database roles, separate Alembic version tables, and `contracts/database/table-ownership.yaml`.
- Neither Python package imports the other package's source.
- Contract changes must pass backward-compatibility checks before backend or frontend merge.
- PostgreSQL migration tests run against PostgreSQL 18, not SQLite.
- No RCA claim may be returned without evidence provenance.
- No MCP call may run while the Incident scope is unclassified.
- UI and AI narrative are zh-TW; raw technical evidence remains unchanged.
- Each plan is committed before the next plan begins.

## Approved-spec coverage

| Design area | Implemented by |
|---|---|
| Repository boundaries, canonical states, OpenAPI, zh-TW shell | Foundation and Contracts |
| PostgreSQL 18, permanent retention, partitions, Grafana auth, dedup, classification, Incident/outbox | Grafana Ingestion and PostgreSQL |
| Standard Grafana normalization, identity v2, scope policy, REST reads, Angular Incident/Alert/RCA UI | Grafana Normalization and Operator UI |
| Independent Worker package, Skills, ADK, endpoint-isolated MCP, specialists, RCA, evidence/provenance | Pub/Sub Emulator and RCA Worker |
| Redaction, telemetry, health, maintenance, containers, timing/failure acceptance | Release Hardening |

## Recommended delivery checkpoints

- **Checkpoint 1:** Foundation and contracts reviewed.
- **Checkpoint 2:** Grafana webhook demo: accepted within two seconds, Incident queryable within five seconds.
- **Checkpoint 3:** Fake-MCP RCA demo: completed or partial within five minutes.
- **Checkpoint 4:** Operator API plus Angular E2E/accessibility review.
- **Checkpoint 5:** Three-package isolation, database grants, security, containers, and SLO release review.
