# Task 3 report — exact UUID-only Worker evidence fidelity

## Scope and provenance

- Baseline for Task 3: `4c3391e`.
- Pre-existing implementation audited here: `9826c1e` (`fix: preserve exact uuid-only worker evidence`).
- `9826c1e` changed exactly five Task 3-owned paths:
  - `rca-worker/src/sre_rca_worker/persistence/repositories/rca.py`
  - `rca-worker/src/sre_rca_worker/application/rca/processor.py`
  - `rca-worker/tests/integration/application/test_persist_evidence.py`
  - `rca-worker/tests/integration/application/test_persist_report.py`
  - `rca-worker/tests/integration/application/test_production_processor.py`
- Fresh-audit follow-up changes are intentionally limited to:
  - `backend/tests/integration/persistence/test_four_stage_migration.py`
  - this report.
- `rca-worker/tests/eval/test_rca_reports.py` and
  `rca-worker/tests/unit/application/test_processor_retry.py` required no change; both were
  included in focused verification. No migration, domain model, Task 4 file, or concurrent
  contracts change was modified.

## Fresh audit of `9826c1e`

The existing commit satisfies the production behavior required by the brief:

- `RcaRepository.insert_evidence()` writes `raw_result BYTEA`, `metadata JSONB`, and
  `content_hash TEXT`; it stores `draft.raw_result` unchanged and hashes those exact bytes.
- Metadata is derived from frozen `EvidenceDraft` fields: content type, input SHA-256, input
  scope, normalized scope, and request-window start/end.
- Evidence ownership and retrieval predicates use only `id`, `rca_run_id`, and
  `specialist_run_id`. No partition helper or synthetic pointer is present in Worker runtime
  source.
- `EvidenceReference` remains frozen, `extra="forbid"`, and contains only `id: UUID`.
  Report citations serialize only `evidenceId` and relation.
- `PersistedEvidence` remains structured-data-only. Repository list/get queries do not select
  raw bytes or metadata, and the canonical smoke asserts these attributes are absent from the
  application object.
- Report persistence writes the draft terminal status to `result_status`. Its version expression
  is `COALESCE((SELECT max(version) ...), 0) + 1`, so the first report receives version 1 without
  mixing an aggregate with unrelated insert expressions.
- The production processor integration proves evidence collection, UUID-only retrieval,
  hypothesis evidence linking, and report persistence.

No forward production fix was necessary. The missing deliverables were the plan-required
four-gate canonical runtime smoke and this report.

## RED and root cause

Fresh RED for the missing acceptance test:

```text
pytest ...::test_clean_four_gate_database_runs_uuid_only_worker_smoke -v
collected 0 items
ERROR: not found ... test_clean_four_gate_database_runs_uuid_only_worker_smoke
exit 4
```

Root cause: `9826c1e` included only the Worker production and three Worker integration test
files. It did not use the brief's additional ownership grant for
`backend/tests/integration/persistence/test_four_stage_migration.py`, so no single acceptance
case joined all four explicit migration gates to the actual Worker repository/report runtime.

The follow-up therefore adds only that missing test. It does not change production code,
migrations, or domain shapes.

## Canonical smoke evidence

`test_clean_four_gate_database_runs_uuid_only_worker_smoke` creates a unique disposable database
and invokes these exact revision targets through `upgrade_four_gates()`:

1. Backend `0002_grafana_normalization_v2`
2. Worker `0002_adk_specialist_analysis`
3. Backend `0003_non_partition_runtime_tables`
4. Worker `0003_validate_ordinary_runtime_tables`

It never invokes `head`, verifies both final version rows, then exercises the real Worker
`RcaRepository` and `ProductionRcaProcessor._persist_report()` boundaries. Direct readback proves:

- exact raw bytes:
  `b'\x00\xffnon-utf8\n{ "b": 1.00, "a": "\\u0061", "a": "duplicate" }\n'`
- exact SHA-256:
  `6816461fc7647701cf69772a3ce29575fa8dbc878bdcab7349bd141ab7ae0ef5`
- provenance object with `contentType=application/json`, `inputSha256="7" * 64`, the safe GCP
  input/normalized scope, and request window `08:15:00Z..08:30:00Z`
- `EvidenceReference` JSON exactly `{"id": "<UUID>"}`
- no `raw_result` or `metadata` attribute on `PersistedEvidence`
- first report `version=1`, `result_status=PARTIAL`, and a UUID-only `evidenceId` hypothesis link.

## Mutation checks

Both required regressions were introduced temporarily and fully reverted after the expected
failure:

- Reverting Worker evidence INSERT to `raw_result_reference` made the canonical smoke fail with
  PostgreSQL `UndefinedColumnError`; the ordinary runtime table has no pointer column.
- Omitting `result_status` from report persistence made the canonical smoke fail with PostgreSQL
  `NotNullViolationError`.

After restoration, neither production file has a working-tree diff.

## Verification

- Five focused Worker files on explicit four-gate disposable databases: initial audit
  `task3_audit_5b31d9c8a12e` **48 passed**, and final post-mutation restoration rerun
  `task3_final_focused_7ce14f390b2a` **48 passed**.
- Full Backend four-stage migration acceptance file, including canonical runtime smoke:
  **10 passed**. Pytest emitted 34 existing Alembic `path_separator` deprecation warnings; these
  originate from Alembic configuration outside Task 3 ownership and do not affect the zero-warning
  static-analysis gates.
- Final canonical-smoke rerun after restoring both mutations: **1 passed**.
- Scoped Backend Ruff: **pass**; scoped Backend Pyright: **0 errors, 0 warnings**.
- Full Backend Ruff: **pass**; full Backend Pyright: **0 errors, 0 warnings**.
- Full Worker Ruff: **pass**; full Worker Pyright: **0 errors, 0 warnings**.
- Worker runtime scan for partition helpers and `raw_result_reference`: **no matches**.
- Canonical-smoke scan for any `command.upgrade(..., "head")`: **no matches**.
- `git diff --check`: **pass**.
- Migration immutability: `git diff 4c3391e -- backend/migrations rca-worker/migrations`
  and the current working-tree migration diff are both empty.
- Cleanup evidence: PostgreSQL listing for `task1_four_stage_%`, `task3_audit_%`, and the exact
  final focused database name returned no database after the test `finally` blocks. Shared
  `sre_agent` was never migrated, truncated, stamped, or dropped.

## Self-review and residual risk

- The canonical smoke imports Worker runtime modules dynamically because it runs in the Backend
  project environment; full Backend Pyright and Ruff validate this boundary.
- Report version allocation preserves the existing run-locking contract. This task does not add
  a new concurrency primitive or change migration/domain shapes.
- Residual risk is limited to the pre-existing Alembic deprecation warning. Changing Alembic
  configuration is outside Task 3 ownership and is not needed for correctness.
- No files are staged or committed by this fresh audit. The follow-up is ready for controller
  review and commit instruction.
