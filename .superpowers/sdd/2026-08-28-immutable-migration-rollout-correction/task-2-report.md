# Task 2 report — Worker 0003 post-conversion validation gate

## Baseline and scope

- Baseline: `bf1ac45abfcd0d53c48e78a572c285b2c002f96b`.
- Owned implementation: `rca-worker/migrations/versions/0003_validate_ordinary_runtime_tables.py`.
- Owned acceptance coverage: `rca-worker/tests/integration/persistence/test_four_stage_conversion.py`, `rca-worker/tests/integration/persistence/test_schema.py`, and `backend/tests/integration/persistence/test_four_stage_migration.py`.
- Immutable files were not changed: Backend `0003_non_partition_runtime_tables`, Worker `0001_rca_worker_v1`, and Worker `0002_adk_specialist_analysis`.

## Inherited-diff audit

The takeover started from four uncommitted owned-path differences left by the first implementer. Each path was re-read against the brief, design, correction plan, frozen migrations, and live PostgreSQL catalog behavior before it was accepted.

- Revision identity is exactly `0003_validate_ordinary_runtime_tables`, with `down_revision = "0002_adk_specialist_analysis"`.
- `upgrade()` requires Backend exactly at `0003_non_partition_runtime_tables` and Worker exactly at `0002_adk_specialist_analysis` before catalog validation.
- Every public catalog precondition runs before the only DDL statement. The final DDL widens only `alembic_version_rca_worker.version_num` from `VARCHAR(32)` to `VARCHAR(64)`, which is required by the 37-character revision slug. The already-64-character Backend version column is not altered by Worker `0003`.
- Wrong-version and damaged-catalog tests snapshot relation, column type/length/nullability, constraint, and version state before the gate; every failure leaves that snapshot unchanged and retains Worker revision `0002_adk_specialist_analysis` with version length 32.
- The revision contains no data DML, replacement table, copy loop, stamp, replay, or `upgrade head`; it only validates the final catalog and performs the necessary Worker version-column widening after all checks pass.
- The FK validation query was found to be insufficiently schema-qualified. A shadow schema produced a true failing regression case; the query now scopes both source and target relations to `public` while still requiring the exact five public UUID-only foreign keys.

## TDD and mutation evidence

- Original Task 2 RED, recorded in the SDD ledger before takeover: Alembic could not resolve revision `0003_validate_ordinary_runtime_tables` because the revision did not exist.
- Takeover RED: `test_worker_0003_ignores_catalog_objects_outside_public_schema` failed with `RuntimeError: dependent foreign keys must be UUID-only` after adding an unrelated `task2_shadow.evidence_records` FK. This proved the production FK query leaked across schemas.
- GREEN: adding source and target `pg_namespace.nspname = 'public'` predicates made the shadow-schema case pass without weakening the exact public FK set.
- Ordinary-PK mutation: replacing `public.evidence_records` with a composite `(id, observed_at)` primary key is rejected with `all canonical runtime tables require ordinary one-column UUID primary keys`; Worker version type/length, revision, and the already-mutated catalog remain otherwise unchanged.
- Additional fail-closed mutations cover a restored partition helper, missing evidence metadata check, missing specialist analysis check, missing lifecycle failure-code check, and composite evidence FK.

## Acceptance results

- Backend four-stage acceptance: `9 passed`; the clean database executes Backend `0002` → Worker `0002` → Backend `0003` → Worker `0003` with explicit revisions.
- Worker conversion plus schema acceptance: `20 passed`; the existing Worker-head path verifies Backend `0002` and Worker `0002`, then executes only Backend `0003` and Worker `0003`.
- Exact persistence evidence is preserved across canonical and retained legacy tables: non-UTF-8/non-canonical JSON raw bytes, JSONB provenance metadata, content hash, report `result_status`, lifecycle failure data, lease/attempt state, and specialist analysis fields.
- Six canonical tables end as ordinary relations with one-column UUID primary keys; six `__partitioned_legacy_0003` parents remain partitioned; partition helpers and composite dependent FKs are rejected.
- Downgrade raises the required forward-gate `RuntimeError` exactly.
- Scoped Ruff and Pyright: Backend owned file and all Worker owned files report zero diagnostics.
- Full Backend Pyright: `0 errors, 0 warnings, 0 informations`.
- Full Worker Pyright: `0 errors, 0 warnings, 0 informations`.
- `git diff --check` passes. Baseline immutable-file diff is empty.

## Database isolation and cleanup

All PostgreSQL integration runs were restricted to `127.0.0.1:5432`. Each test creates a UUID-suffixed database named under `task1_four_stage_`, `task2_worker_gate_`, or `task2_worker_schema_`, and force-drops that exact name in `finally`. A final read-only query of `pg_database` returned no databases with those prefixes. The shared `sre_agent` database was never migrated, truncated, stamped, or dropped.

## Self-review and residual risk

- Reviewed the full owned diff for version-gate ordering, public-schema qualification, catalog exactness, data preservation, migration immutability, and forbidden DML/stamping/head usage.
- The successful final catalog explicitly proves Worker version length 64 and that Backend version length remains 64 across the Worker gate.
- Tests emit Alembic's existing `path_separator` deprecation warning; this is configuration noise outside Task 2 ownership and does not affect migration behavior.
- No known functional or release-blocking risk remains within Task 2 scope.
