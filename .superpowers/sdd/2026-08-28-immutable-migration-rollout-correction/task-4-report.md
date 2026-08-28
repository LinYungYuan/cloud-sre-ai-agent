# Task 4 audit report — remove standalone Outbox and Partition runtimes

## Scope and audited commit

- Fresh audit target: `93aefb98b4f32ab3e92f46fc2dbd7d1528e613d0`
  (`refactor: remove outbox and partition workers`).
- Worktree: `/Users/linyungyuan/Desktop/sre-agent2.0/.worktrees/backend-runtime-simplification`.
- This audit did not modify migrations, deployment manifests, or the uncommitted
  `contracts/compatibility-tests/test_gke_manifests.py` work.

`git diff-tree --name-status 93aefb9` shows exactly the Task 4 deletion set:
the standalone Outbox settings/main/worker, partition worker/helper, and their
dedicated tests; `backend/pyproject.toml`; and the design consistency contract.
No migration or deployment manifest appears in that commit.

## Contract and retention audit

The commit removes only obsolete standalone runtime ownership:

- no `sre-agent-outbox-worker` or `sre-agent-ensure-partitions` console script;
- no `OutboxSettings`, partition helper, `outbox_main`, `outbox_worker`, or
  `partition_worker` Python runtime source/import; and
- no retained source file for the five obsolete runtime modules.

It preserves the required request/API path:

- `application/outbox/publish_events.py` (`OutboxPublishService`);
- `application/outbox/recover_events.py` and the three protected recovery routes;
- `integrations/pubsub/publisher.py` and Backend lifecycle composition;
- request-transaction post-commit `publish_event(event_id)` in
  `IngestGrafanaAlerts`; and
- durable `outbox_events` persistence (not touched by `93aefb9`).

The audit strengthened `contracts/compatibility-tests/test_design_consistency.py`
to name all five removed module paths, include `outbox_worker` in the residue scan,
and require the three retained outbox/Pub/Sub module paths.  RED evidence is an
actual execution of the new assertion against a temporary `git archive 93aefb9^`
tree: it failed with all five obsolete module paths.  GREEN evidence is the current
design/contract test run below.

## Verification

- Focused design, publish, recovery, webhook, and recovery-route tests:
  `54 passed`.
- Exact runtime symbol/script and import scans: no matches.
- PostgreSQL publish integration at `127.0.0.1:5432`: all four
  `backend/tests/integration/application/outbox/test_publish_events.py` tests
  passed, including explicit row locking, status-specific batches, stable order,
  and `SKIP LOCKED` behavior.  The test uses its existing test database fixture.
- Full Backend Ruff (`backend/src`, `backend/tests`, and this contract):
  `All checks passed`.
- Full Backend Pyright (`backend/src`, `backend/tests`, and this contract):
  `0 errors, 0 warnings, 0 informations`.
- `git diff --check 93aefb9^ 93aefb9`: passed.

### Remaining integration fixture failure outside Task 4

The combined publish/ingestion integration command reached the database under the
approved `127.0.0.1:5432` connection.  The four Outbox publisher tests passed.
All 14 ingestion nodes error during fixture setup, before any assertion or Task 4
runtime code runs, because
`backend/tests/integration/_disposable_database.py` upgrades a disposable database
directly to Backend `0003_non_partition_runtime_tables` without first applying
Worker `0002_adk_specialist_analysis`.  Backend `0003` correctly fails closed with:

```text
RuntimeError: Worker 0002_adk_specialist_analysis is required
```

This is a four-gate fixture setup issue owned outside Task 4; no migration or
ingestion fixture was changed here.

### Full non-integration suite failures outside Task 4

`uv run --project backend pytest backend/tests/unit backend/tests/contract
contracts/compatibility-tests -q` produced `294 passed, 7 failed`.

- `contracts/compatibility-tests/test_contracts.py::test_every_existing_table_has_one_migration_owner`
  fails at line 651 because it asserts that every post-0001 Backend migration does
  not reference a Worker-owned table; `0003_non_partition_runtime_tables.py`
  intentionally references those tables for the approved conversion.
- The following six nodes are intentionally left for the retained Task 5 manifest
  work in `contracts/compatibility-tests/test_gke_manifests.py` and are not modified
  by this audit:
  - `test_kustomization_references_each_base_manifest` — stale outbox deployment
    and partition CronJob resources remain in the manifest.
  - `test_base_contains_only_portable_application_resources` — workload counts still
    include those two obsolete resources.
  - `test_removed_outbox_and_partition_workloads_are_absent_from_base` —
    `sre-agent-outbox` remains present.
  - `test_every_workload_has_hardened_non_root_security_and_resource_limits` — the
    obsolete workload names are extra.
  - `test_backend_owns_pubsub_publisher_config_but_excludes_ai_and_mcp_settings` —
    Backend still has `MODEL_NAME` and three MCP URL settings.
  - `test_release_runbook_fails_fast_and_orders_migrations_before_rollouts` — the
    release section still rolls out `sre-agent-outbox`.

## Changed paths and risk

Uncommitted changes from this audit are limited to:

- `contracts/compatibility-tests/test_design_consistency.py`
- `.superpowers/sdd/2026-08-28-immutable-migration-rollout-correction/task-4-report.md`

Risk remaining in Task 4 itself is limited to the ingestion integration fixture
being unable to exercise the full four-gate schema setup until its owning task
updates it.  The standalone publisher integration, static removal/retention
contract, request-level contract behavior, full Ruff, and full Pyright are green.
