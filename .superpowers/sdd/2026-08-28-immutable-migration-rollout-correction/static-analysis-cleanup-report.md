# Backend static-analysis cleanup report

## Scope

- Baseline: `599a9dcd087d1aa2d80a87eb17eccbad719b3770`
- Changed only the three routed Backend test files and this report.
- No production, migration, configuration, manifest, or shared database state changed.
- Two earlier cleanup owners were recorded as leaving no work, but their delayed shared
  writes appeared after this task started. Those diffs were preserved, audited against
  the production interfaces, mutation-checked, and minimally adjusted for Ruff.

## Reproduced RED diagnostics

Temporarily reversing the three delayed test-only changes and running full Backend
Pyright reproduced exactly the routed baseline:

1. `tests/contract/api/test_operator_reads.py:375` —
   `reportIncompatibleMethodOverride`: the unannotated base fake inferred
   `dict[str, dict[str, Unknown]]`, while the absent-trace subclass inferred
   `dict[str, None]`.
2. `tests/integration/persistence/test_alembic_version_reconciliation.py:63` —
   `reportAttributeAccessIssue`: `ModuleType` does not statically declare the dynamic
   `context` attribute used by the synthetic Alembic module.
3. `tests/unit/application/outbox/test_publish_events.py:166` —
   `reportArgumentType`: the behavioral `_FakeSessionFactory` was structurally usable
   through `begin()` but was not the nominal `async_sessionmaker[AsyncSession]` required
   by `OutboxPublishService`.

RED result: `3 errors, 0 warnings, 0 informations`.

## Root causes and minimal fixes

- The operator fake now explicitly returns `dict[str, Any]` in both base and override,
  matching `OperatorReadService.get_trace_waterfall` and the real repository. Response
  fixtures and route assertions are unchanged.
- The synthetic Alembic module injects its single dynamic `context` boundary through
  `alembic.__dict__`. This reflects Python module behavior without a suppression or
  broad cast.
- The outbox helper creates a nominal `async_sessionmaker[AsyncSession]` and injects
  only its `begin` boundary through the instance dictionary. The existing fake rows,
  SQL capture, transaction commit behavior, publisher behavior, and assertions remain
  authoritative. Production typing and behavior are unchanged.
- Initial delayed writes used constant-name `setattr`, which satisfied Pyright but
  failed Ruff B010. The explicit `__dict__` form resolves both tools without ignores.

No `Any` was added around an entire fake, no `type: ignore`, blanket suppression,
production protocol change, or assertion weakening was introduced.

## GREEN evidence

The three complete affected test files ran against the unique disposable database
`static_analysis_cleanup_01a0324a84c67703`, with an EXIT trap dropping that exact
database regardless of test outcome:

```text
27 passed in 10.44s
```

Static checks:

```text
uv run ruff check <three owned test files>
All checks passed!

uv run pyright <three owned test files>
0 errors, 0 warnings, 0 informations

uv run pyright
0 errors, 0 warnings, 0 informations
```

## Self-review and residual risk

- All runtime behavior remains exercised by the same 27 tests; only test-double type
  expression changed.
- The outbox adapter deliberately patches only `begin`, the sole session-factory
  operation used by `OutboxPublishService`. If production expands that concrete
  dependency, Pyright may still pass while the behavior tests fail, which is the
  desired signal to revisit the service boundary rather than broaden this adapter.
- No shared `sre_agent` schema or data was used; integration checks ran only in the
  disposable database and the cleanup trap completed with command exit 0.
- Full Backend Pyright is now a genuine zero-error, zero-warning gate.
