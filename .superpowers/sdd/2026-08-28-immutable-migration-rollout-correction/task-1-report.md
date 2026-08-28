# Task 1 report — preserve Worker evidence in Backend 0003

## Scope and baseline

- Worktree: `/Users/linyungyuan/Desktop/sre-agent2.0/.worktrees/backend-runtime-simplification`
- Baseline commit: `8755126a671cf0aac85bea5eb0cf51bca8631ac6`
- Owned implementation/test files were limited to the Task 1 brief. Worker migrations
  `0001_rca_worker_v1` and `0002_adk_specialist_analysis` were read as the immutable
  source contract and were not modified.
- Every PostgreSQL case used a unique `task1_four_stage_<uuid>` or
  `task7_migration_<uuid>` disposable database and dropped that exact database in
  `finally`. The shared `sre_agent` database was not migrated, truncated, or dropped.

## Inherited RED evidence and root cause

The takeover began with the substantial uncommitted diff recorded in `progress.md`.
The authentic source contract is established by running Backend
`0002_grafana_normalization_v2`, Worker `0001_rca_worker_v1`, and Worker
`0002_adk_specialist_analysis`. Worker 0001 removes `raw_result_reference` and replaces
it with non-null `raw_result BYTEA` and `metadata JSONB`; Worker 0002 retains those
columns while extending lifecycle and specialist-analysis checks.

The inherited regression test was mutation-checked against the baseline Backend 0003
evidence replacement/copy shape. The focused command failed at the Backend 0003 copy
with the expected real PostgreSQL error:

```text
asyncpg.exceptions.UndefinedColumnError:
column "raw_result_reference" does not exist
```

The failing statement selected `raw_result_reference FROM evidence_records` after the
real Worker 0001/0002 migrations had removed that column. The disposable database was
still removed by the context manager's `finally` path.

## Implemented correction and acceptance coverage

- Backend 0003 replacement DDL and copy SQL now preserve `raw_result BYTEA NOT NULL`,
  `metadata JSONB NOT NULL`, `content_hash TEXT NOT NULL`, and the metadata-object
  check verbatim; `raw_result_reference` is absent from the target.
- A preflight runs before duplicate checks or replacement creation. It requires the
  exact Backend 0002 and Worker 0002 revisions and validates Worker-owned evidence,
  report-status, lifecycle, worker-claim, specialist-analysis columns, checks, and
  index contracts.
- Fail-closed integration cases cover Worker 0001, missing evidence metadata, missing
  report-status check, missing worker attempt/lifecycle fields, missing lifecycle
  failure-code fields, missing specialist-analysis checks, and duplicate evidence
  UUIDs. Each failure proves the canonical evidence table OID is unchanged and no
  `evidence_records_new` table remains.
- The only accepted rerun is Alembic at matching Backend 0003; its evidence OID and
  Worker catalog snapshot remain unchanged.
- The real two-month fixture preserves exact non-UTF-8/non-canonical bytes,
  provenance metadata, content hash, `rca_reports.result_status`, lifecycle failure
  codes and attempt/lease data, plus the complete specialist analysis row. Worker-owned
  table OIDs, columns, constraints, indexes, and data snapshots are unchanged.
- Existing ordinary-table acceptance still proves all six canonical relations are
  ordinary UUID-primary-key tables, retained legacy parents remain partitioned, UUID-only
  foreign keys work, and new canonical writes do not enter the legacy parents.
- The shared test helper invokes only explicit revision targets and never uses
  `upgrade head` or stamping.

## Verification evidence

### Focused GREEN

```text
MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/sre_agent \
uv run pytest \
  tests/integration/persistence/test_four_stage_migration.py \
  tests/integration/persistence/test_non_partition_migration.py \
  tests/unit/persistence/test_schema_documentation.py -v

20 passed, 34 warnings in 52.89s
```

The warnings are Alembic's existing `path_separator` deprecation warnings; no test
failed and no warning is produced by the Task 1 implementation.

### Ruff and Pyright

```text
uv run ruff check <four Task 1 Python files>
All checks passed!

uv run pyright <four Task 1 Python files>
0 errors, 0 warnings, 0 informations
```

The first full Backend Pyright run reported six errors: three were in the owned
`test_non_partition_migration.py` fixture annotation/narrowing and were fixed. A fresh
full Backend run then reported exactly three remaining errors, all routed to their
owners and intentionally not modified by Task 1:

- `tests/contract/api/test_operator_reads.py:375` — incompatible fake override return.
- `tests/integration/persistence/test_alembic_version_reconciliation.py:63` — dynamic
  `ModuleType.context` assignment.
- `tests/unit/application/outbox/test_publish_events.py:166` — fake session factory
  type mismatch.

Fresh full result: `3 errors, 0 warnings, 0 informations`. This is the only repository
gate not green within Task 1 ownership; the Task 1 scoped gate is green.

### Diff and ownership checks

- `git diff --check`: exit 0.
- Worker 0001/0002 diff: empty.
- Changed implementation/test paths are the four Task 1-owned files plus this report.

## Self-review and residual risk

- The preflight is intentionally strict and depends on the immutable Worker 0001/0002
  constraint/index names and semantic allowlists. This is fail-closed by design; any
  independently altered source catalog requires operator review rather than migration.
- Backend 0003 remains a one-way forward conversion. Renamed partitioned parents remain
  as rollback sources, but no lossless downgrade is claimed after ordinary-table writes.
- No cleanup of legacy parents is performed.
- Full Backend Pyright remains blocked by the three routed, unowned errors listed above;
  there are no remaining Task 1 scoped static-check errors.
