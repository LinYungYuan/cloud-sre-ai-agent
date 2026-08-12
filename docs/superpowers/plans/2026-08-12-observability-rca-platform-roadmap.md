# Observability RCA Multi-Agent Platform Implementation Roadmap

> **For agentic workers:** Execute the linked plans in order. Each plan is independently reviewable and ends in working, tested software.

**Goal:** Deliver the approved SRE AI Agent platform without coupling Angular, the operator API, Grafana ingestion, or the RCA runtime.

**Architecture:** A monorepo contains an independently deployable FastAPI API, Python RCA worker, and Angular SPA. Versioned OpenAPI and SSE schemas are the only frontend/backend boundary; Cloud SQL PostgreSQL 18 is the source of truth and Pub/Sub carries durable jobs through a transactional outbox.

**Approved design:** `docs/superpowers/specs/2026-08-12-observability-rca-platform-design.md`

## Execution order

1. `2026-08-12-platform-foundation-contracts-plan.md`
   - Create repository scaffolding, backend quality gates, canonical domain types, and versioned API/event contracts.
   - Exit condition: schemas validate and both projects can be built/tested independently.
2. `2026-08-12-grafana-ingestion-database-plan.md`
   - Create the PostgreSQL 18 schema, Grafana authentication/normalization, deduplication, classification, Incident lifecycle, outbox, and ingestion endpoint.
   - Exit condition: a real Grafana fixture produces exactly one durable Incident and one RCA job under redelivery.
3. `2026-08-12-rca-agent-worker-plan.md`
   - Create the worker, MCP capability boundaries, specialist execution, evidence/provenance persistence, hypothesis synthesis, deadlines, retries, and partial reports.
   - Exit condition: deterministic fake MCP data produces an evidence-backed zh-TW RCA report and survives redelivery.
4. `2026-08-12-operator-api-realtime-plan.md`
   - Create scope authorization, Incident/Alert/RCA/conversation operations, optimistic concurrency, cursor pagination, audit logs, and replayable SSE.
   - Exit condition: all operator flows pass API contract and authorization tests.
5. `2026-08-12-angular-operator-ui-plan.md`
   - Create the zh-TW Angular SPA, generated API client, realtime service, dashboard, Incident investigation, alert classification, mappings, and E2E tests.
   - Exit condition: the critical user journey passes independently against the published backend contract.
6. `2026-08-12-release-hardening-plan.md`
   - Add health/readiness, structured redacted telemetry, partition maintenance, admin bootstrap commands, independent containers, and end-to-end SLO verification.
   - Exit condition: API, worker, and Angular images pass independent smoke tests and the three timing objectives are measured.

## Cross-plan release gates

- Never add an `infrastructure/` directory, Terraform, or Kubernetes manifests.
- Backend, worker, and frontend have separate build/test commands and Dockerfiles.
- Contract changes must pass backward-compatibility checks before backend or frontend merge.
- PostgreSQL migration tests run against PostgreSQL 18, not SQLite.
- No RCA claim may be returned without evidence provenance.
- No MCP call may run while the Incident scope is unclassified.
- UI and AI narrative are zh-TW; raw technical evidence remains unchanged.
- Each plan is committed before the next plan begins.

## Approved-spec coverage

| Design area | Implemented by |
|---|---|
| Repository boundaries, canonical states, OpenAPI/SSE, zh-TW shell | Foundation and Contracts |
| PostgreSQL 18, permanent retention, partitions, Grafana auth, dedup, classification, Incident/outbox | Grafana Ingestion and PostgreSQL |
| Skills, Hybrid Router, ADK, endpoint-isolated MCP, specialists, RCA, evidence/provenance, shared follow-up | RCA Agent Worker |
| Identity abstraction, scope policy, REST operations/reads, audit, cursor pagination, realtime replay | Operator API and Realtime |
| Dashboard, Incident/Alert UI, shared chat, RCA/evidence, unclassified/mappings, zh-TW/accessibility | Angular Operator UI |
| Redaction, telemetry, health, maintenance, containers, timing/failure acceptance | Release Hardening |

## Recommended delivery checkpoints

- **Checkpoint 1:** Foundation and contracts reviewed.
- **Checkpoint 2:** Grafana webhook demo: accepted within two seconds, Incident queryable within five seconds.
- **Checkpoint 3:** Fake-MCP RCA demo: completed or partial within five minutes.
- **Checkpoint 4:** Operator API security and concurrency review.
- **Checkpoint 5:** Angular E2E and accessibility review.
- **Checkpoint 6:** Security, observability, container, and SLO release review.
