# Task 5 final audit report — Compose and Kubernetes runtime ownership

## Scope and provenance

- Final-audit baseline: `136d3f9`.
- Audited implementation commit: `4738f88` (`refactor: simplify runtime deployments`).
- This follow-up remains uncommitted and unstaged pending controller authorization.
- This task's ownership was expanded by controller ruling only for:
  - the two stale Compose assertions in `contracts/compatibility-tests/test_contracts.py`;
  - replacement of the two ambiguous migration Job templates with four exact-gate Job templates.
- No migration source, runtime source, fixture, Task 6 document, or Task 7 path was modified.

## Audit findings

### Confirmed Compose defect

Commit `4738f88` did not modify `docker-compose.yml`. PostgreSQL therefore still rendered host port
`55432`, contradicting `.env.compose.example` (`POSTGRES_HOST_PORT=5432`) and the required mapping
`${POSTGRES_HOST_PORT:-5432}:5432`. Pub/Sub used the correct numeric port but remained hard-coded,
so it could not honor `PUBSUB_HOST_PORT` overrides.

The previous report incorrectly called rendered PostgreSQL port `55432` correct. The corrected
contract requires default/example ports `5432` and `58085`, and proves custom overrides are honored.
Compose renders only `postgres` and `pubsub-emulator`; it has no application service or application
`env_file`.

### Critical migration Job defect

The two inherited Job templates both ran `alembic upgrade head`. That could cross unverified gates and
violated the four-stage rollout requirement. They were replaced with four separately creatable Jobs:

| Manifest | Service account | Exact target |
|---|---|---|
| `backend-0002-migration-job.yaml` | `sre-agent-backend` | `0002_grafana_normalization_v2` |
| `worker-0002-migration-job.yaml` | `sre-agent-rca-worker` | `0002_adk_specialist_analysis` |
| `backend-0003-migration-job.yaml` | `sre-agent-backend` | `0003_non_partition_runtime_tables` |
| `worker-0003-migration-job.yaml` | `sre-agent-rca-worker` | `0003_validate_ordinary_runtime_tables` |

Each Job retains bounded execution, non-root/read-only security, resource requests/limits,
stream-specific image and KSA, and exactly one `DATABASE_URL` Secret reference. No Job contains AI/MCP,
Grafana, publisher/subscriber, `head`, or `stamp` configuration.

### Runtime ownership and deletion audit

- Backend env is asserted as the exact set `DATABASE_URL`, `GRAFANA_TOKENS`,
  `PUBSUB_PROJECT_ID`, `RCA_TOPIC_ID`, and `APP_ENVIRONMENT`.
- Worker env is asserted as the exact runtime settings set required by its subscriber/bootstrap,
  AI/MCP, specialist-analysis, evidence-budget, deadline, and worker identity interfaces; it has no
  `GRAFANA_TOKENS` Secret.
- Backend and Worker use distinct KSAs. The deployment runbook assigns publisher IAM to Backend and
  subscriber/model/MCP IAM to Worker; portable base ServiceAccounts intentionally contain no
  environment-specific IAM annotations.
- Rendered base contains exactly two ServiceAccounts, one ConfigMap, two Services, and three
  Deployments. It contains no Outbox Deployment, Partition CronJob, deleted service account, RBAC
  residue, Secret, Ingress, or cloud-provider-specific resource.
- The contract now rejects unexpected Backend or Worker env keys rather than using subset assertions
  with blind spots.

## TDD evidence

### Compose RED → GREEN

After adding the render-based contract, the unmodified Compose file produced:

```text
1 failed, 13 passed
expected ('5432', 5432), rendered ('55432', 5432)
```

After the minimal port interpolation fix:

```text
14 passed
```

The combined contracts then exposed two stale raw-string assertions in `test_contracts.py` that still
required `127.0.0.1:55432:5432` and the fixed Pub/Sub mapping. Following an explicit controller
ownership ruling, both were converted to render-based default and custom-override assertions:

```text
2 passed
```

### Exact-gate Job RED → GREEN

The new Job contract first failed because only the two old generic filenames existed and the four exact
gate manifests were absent. After replacing them, the targeted contract passed:

```text
1 passed
```

## Render evidence

`docker compose --env-file .env.compose.example config` renders:

- PostgreSQL target/published port `5432/5432`;
- Pub/Sub target/published port `8085/58085`;
- only the two infrastructure services and no application environment injection.

The same contract renders with `POSTGRES_HOST_PORT=15432` and `PUBSUB_HOST_PORT=18085` and observes
those exact published ports, proving interpolation rather than a matching hard-coded default.

`kubectl kustomize deploy/k8s/base` renders:

```text
ServiceAccount/sre-agent-backend
ServiceAccount/sre-agent-rca-worker
ConfigMap/sre-agent-config
Service/sre-agent-backend
Service/sre-agent-frontend
Deployment/sre-agent-backend
Deployment/sre-agent-frontend
Deployment/sre-agent-rca-worker
```

The four Job documents are parsed and validated by the manifest contract. An additional
`kubectl create --dry-run=client --validate=false -f deploy/k8s/jobs` attempt could not be used as an
offline renderer because this kubectl build still attempted discovery against the configured local
cluster, which the sandbox denied. No cluster state was changed.

## Verification

Final command results are recorded after the last file change:

| Gate | Result |
|---|---|
| Task 5 manifest contract | included in 55-case run; passed |
| All compatibility/design contracts | 55 passed |
| Docker Compose render | passed; published `5432` / `58085` |
| Kustomize base render | passed; 8 expected resources, no deleted workloads |
| Ruff | all checks passed |
| Full Backend Pyright | 0 errors, 0 warnings, 0 informations |
| YAML/job exact-target checks | passed for all four manifests |
| Immutable migration diff | clean |
| `git diff --check` | clean |

## Sequential handoff and risks

- Per controller instruction, `deploy/k8s/README.md` was not modified in this task. At this intermediate
  point it still names the two deleted generic Job paths. Task 6 must update the runbook to invoke all
  four new manifests in exact gate order before release. This is an explicit sequential handoff, not a
  waiver; Task 5 and the corresponding Task 6 correction must land together for a deployable tree.
- Removing the loopback host prefix follows the controlling Task 5 mapping decision exactly. PostgreSQL
  still uses trust authentication for local development; operators must not expose this Compose stack on
  an untrusted host/network.
- No other implementation risk was found inside the audited Task 5 ownership boundary.
