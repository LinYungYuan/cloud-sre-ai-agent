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
