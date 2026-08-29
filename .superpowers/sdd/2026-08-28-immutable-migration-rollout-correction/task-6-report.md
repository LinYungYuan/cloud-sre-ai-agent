# Task 6 report — immutable rollout documentation fix

## Provenance

- Original documentation commit: `9ded769` (`docs: document immutable migration rollout`).
- Fix baseline: `83062de` (`fix: enforce explicit deployment gates`).
- Trigger: independent Task 6 review rejected `9ded769` for two Critical and
  three Important documentation-contract failures.
- This uncommitted fix changes only the Task 6 brief's owned documentation and
  contract files. It does not change Jobs, runtime code, migrations, or Compose.

## RED evidence

After strengthening the document and runbook contracts against the current
documents, the focused run produced **4 failed, 20 passed**:

1. The authoritative four-gate maintenance-window section did not exist.
2. The root operator documentation omitted all three protected outbox recovery
   paths and the no-replay/no-override policy.
3. The schema document lacked one final UUID-only runtime-schema section.
4. The Kubernetes runbook still used the removed generic Backend/Worker Job
   names instead of the committed four exact-target Job manifests.

The all-document `alembic upgrade head`/stamp guard was already green at this
baseline because Task 5 had corrected the committed Job manifests. The fix also
removed the remaining worker container example that offered a head upgrade.

## Fix

- `docs/database/postgresql-schema.md` is now the single authoritative
  maintenance-window procedure: stop writes, run Backend-0002 → Worker-0002
  → Backend-0003 → Worker-0003, query both version tables after every gate,
  keep runtimes stopped through Worker-0003 catalog checks, handle existing
  Worker-0002 head databases without replay/stamp, and use forward-only backup
  or delta recovery after new writes.
- The schema reference now has one internally consistent final UUID-only
  runtime section for all six canonical ordinary tables, exact durable evidence
  fields, report status/lifecycle/analysis ownership, UUID-only references,
  canonical `relkind='r'` catalog check, exact six retained legacy parent
  names, and explicit historical-row copy/validation before cutover.
- `README.md` documents the protected single-event and bounded pending/failed
  recovery endpoints, global authorization, no startup/periodic/incidental
  replay, and no payload/topic/project/subscription/scope overrides.
- `backend/README.md` and `rca-worker/README.md` link their operator actions to
  the authoritative procedure; the Worker image example now uses `current`,
  not an unsafe migration shortcut. Local example database endpoints use port
  5432.
- `deploy/k8s/README.md` creates and awaits the four actual committed Job
  manifests in the required interleaved order, requires per-gate checks before
  the next Job, and defers base/runtime rollout until the fourth postcondition.
- `test_design_consistency.py` now asserts the real operational contract, not
  mere substring presence. `test_gke_manifests.py` exact-matches the four Job
  create/wait pairs and rejects generic names, head, and stamp in the runbook.

## GREEN evidence

| Gate | Result |
| --- | --- |
| Focused design + GKE documentation contracts | 24 passed |
| Full `contracts/compatibility-tests` | 56 passed |
| Backend Ruff | passed |
| RCA Worker Ruff | passed |
| Backend Pyright | 0 errors, 0 warnings, 0 informations |
| RCA Worker Pyright | completed successfully with no diagnostics |
| Operator stale-text scan | no matches for unsafe head/stamp, canonical pointer, removed runtimes, wrong legacy name, or automatic replay claims |
| Immutable migration path diff | no changed migration paths |
| `git diff --check` | clean |

## Changed paths

- `README.md`
- `backend/README.md`
- `rca-worker/README.md`
- `deploy/k8s/README.md`
- `docs/database/postgresql-schema.md`
- `contracts/compatibility-tests/test_design_consistency.py`
- `contracts/compatibility-tests/test_gke_manifests.py`
- `.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-6-report.md`

## Self-review and risks

- The documentation refers to the committed Task 5 filenames
  `backend-0002-migration-job.yaml`, `worker-0002-migration-job.yaml`,
  `backend-0003-migration-job.yaml`, and `worker-0003-migration-job.yaml`; it
  does not guess or recreate Job configuration.
- No operator-facing migration instruction uses head or stamp. No procedure
  permits runtime writes between gates or automatic outbox replay.
- Legacy parents remain retained. The documented rollback policy does not claim
  a lossless post-write downgrade.
- This worktree remains **READY_TO_COMMIT** only: nothing has been staged or
  committed, pending controller authorization.

## Round 2 — schema reference completeness correction

### Trigger and root cause

Task 7 reran the Backend gate from `b5f20e6` and found a deterministic schema
reference regression: **338 passed, 6 failed** in the full Backend suite, and
the focused schema-documentation module was **6 failed, 3 passed**. The
four-gate rewrite correctly made the final six runtime relations authoritative,
but accidentally removed the evolving reference's immutable Backend-0001
historical inventory, operational indexes, and Backend-0002 normalization-v2
fragments. No production code, Job, Compose, or migration behavior was at
fault.

The six failures mapped one-to-one to the missing reference material:

1. Historical Backend-0001 table manifest no longer contained every migration
   parent table.
2. The same missing table inventory violated the parent-table coverage check.
3. The 15 required operational indexes were absent.
4. The final `webhook_deliveries` reference had lost the accepted
   `VALIDATION_FAILED` delivery status.
5. Validation/default fields and incident identity-v2 fields were absent.
6. Backend-0002 normalization rules, folder mapping, column alterations, and
   lookup-index fragments were absent.

### Minimal correction

Only `docs/database/postgresql-schema.md` changed in this round. It now keeps a
clearly labelled, non-runtime historical Backend-0001/0002 manifest alongside
the approved final UUID-only six-table section. The historical pointer field is
explicitly historical; the canonical evidence interface remains
`raw_result BYTEA`, object `metadata JSONB`, and `content_hash`. The final
delivery and alert table definitions now reflect the actual Backend-0003
replacement contract, including validation and normalization columns. The
legacy partition-parent section and forward-only rollout procedure remain
unchanged.

### Round-2 GREEN evidence

| Gate | Result |
| --- | --- |
| Focused `test_schema_documentation.py` | 9 passed |
| Full Backend suite (explicit local test DB URL on port 5432) | 344 passed, 40 warnings |
| Full `contracts/compatibility-tests` | 56 passed |
| Backend Ruff | passed |
| RCA Worker Ruff | passed |
| Backend Pyright | 0 errors, 0 warnings, 0 informations |
| RCA Worker Pyright | completed successfully with no diagnostics |
| Operator stale-text scan | no matches |
| Immutable migration path diff | no changed migration paths |
| `git diff --check` | clean |

This round is **READY_TO_COMMIT**: no files are staged and no new commit has
been made.

## Round 3 — immutable Backend-0001/0002 section boundary correction

### Trigger, TDD RED, and root cause

Round-2 review found one Important contradiction: the labelled Backend-0001
inventory had already made `incidents.team_id`, `project_id`, and
`environment_id` nullable and included the Backend-0002-only
`identity_version`, `provider`, `folder_code`, and `alert_name` columns. It
then represented some of the same changes again as Backend-0002 `ALTER`s.

The new section-bounded regression was written first and produced the expected
RED: **1 failed** because the document had no separate
`### Backend-0001 published baseline` and
`### Backend-0002 normalization and identity mutations` boundaries. The test
asserts the published 0001 scope `NOT NULL` contract and absence of every
identity-v2 column, then asserts all four `ADD COLUMN` operations and all
three `DROP NOT NULL` operations in only the 0002 block, plus post-state
presence in the final section.

### Minimal correction and GREEN evidence

Only the owned schema document and its regression test changed. The 0001
`incidents` inventory now reflects its published baseline exactly for the
three scope columns and excludes every 0002-only column. The 0002 block now
contains each identity-v2 `ADD COLUMN` and the original nullability changes;
the final UUID-only section describes the post-0002 Incident state without
altering its six-table canonical or retained-legacy contracts.

| Gate | Result |
| --- | --- |
| Focused schema-documentation module | 10 passed |
| Full Backend suite (explicit local test DB URL on port 5432) | 345 passed, 40 warnings |
| Full `contracts/compatibility-tests` | 56 passed |
| Backend Ruff / RCA Worker Ruff | passed / passed |
| Backend Pyright / RCA Worker Pyright | 0 errors, 0 warnings / 0 errors, 0 warnings |
| Operator stale-text scan | no matches |
| Immutable migration path diff | no changed migration paths |
| `git diff --check` | clean |

Round 3 is **READY_TO_COMMIT**: no files are staged and no commit has been
created for this round.

## Round 4 — cross-stream migration-order contract restoration

### Trigger, RED evidence, and root cause

Task 7's fresh Worker suite reported **1 failed, 429 passed, 1 skipped**. The
focused failure was
`test_schema_documentation_describes_worker_migration_order_and_data_loss`:
the authoritative schema reference no longer contained the exact phrase
`Backend migration → RCA Worker migration`. `git log -S` traced that removal
to Task 6 commit `b5f20e6`, which replaced the earlier overview while adding
the correct four-gate procedure. After restoring the phrase contiguously, the
same focused test exposed its next previously masked assertion: the exact
`無法還原` data-loss warning had also been removed by `b5f20e6`.

### Minimal correction

Only `docs/database/postgresql-schema.md` changed. Its introduction now states
the required cross-stream order and maps it truthfully to both interleaved
pairs: Backend-0002 before Worker-0002, then Backend-0003 before Worker-0003.
The existing forward-only recovery paragraph now states that the exact
pre-conversion state is `無法還原` through Alembic downgrade while preserving
the prohibition on downgrade and the approved-backup/validated-delta recovery
paths. No gate command, version query, maintenance-window rule, retained-legacy
policy, or executable behavior changed.

### Round-4 GREEN evidence

| Gate | Result |
| --- | --- |
| RCA Worker focused schema-documentation test | 1 passed |
| Backend focused schema-documentation module | 10 passed |
| Full `contracts/compatibility-tests` | 56 passed |
| Operator stale-text scan | no matches for unsafe head/stamp, removed runtime commands, or lossless downgrade claims |
| `git diff --check` | clean |

### Self-review

- The tracked diff contains exactly two documentation clarifications in the
  owned schema reference; the ignored report is the only other edited path.
- All four explicit revision commands and all eight per-gate version queries
  remain byte-for-byte present and in their original order.
- The maintenance-window, existing Worker-0002, no-stamp/no-replay, retained
  legacy parents, and forward-only recovery policies remain intact.
- No test, migration, runtime, manifest, `CURRENT.md`, or `progress.md` path was
  modified.
