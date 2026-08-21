# GKE release runbook

`deploy/k8s/base` is a portable application base. It deliberately does not
choose a namespace or registry and does not contain Google service-account
annotations, Secret data, Gateway resources, or Terraform-managed cloud
resources. An environment-specific release process must supply those inputs.

## Release inputs and preflight

Before running a release, confirm that Terraform or another infrastructure
process has made Cloud SQL, the RCA Pub/Sub topic and subscription, networking,
and the required Workload Identity bindings available. The release tool must
also prepare the environment's namespace, Gateway, registry images, and the
existing `sre-agent-secrets` Secret.

The checked-in manifests use local image names ending in `:latest` only as
Kustomize transformation keys. Before creating either migration Job, the
release tool must apply Kustomize `images` overrides to the base and Job
manifests and materialize a release bundle whose images use immutable digests,
for example `REGISTRY/sre-agent-backend@sha256:...`. Do not deploy the checked-in
`:latest` references. The ordered commands below assume those exact paths refer
to the materialized release bundle.

Select the intended cluster context and namespace first. An empty namespace
means the context would use `default`; do not continue unless that is explicitly
the target.

```bash
kubectl config current-context
TARGET_NAMESPACE=$(kubectl config view --minify -o jsonpath='{..namespace}')
test -n "${TARGET_NAMESPACE}"
kubectl get namespace "${TARGET_NAMESPACE}"
kubectl get secret sre-agent-secrets
```

The pre-existing `sre-agent-secrets` must contain these keys; never print or
commit their values:

- `DATABASE_URL`: shared PostgreSQL connection URL used by the two migrations,
  Backend, outbox publisher, Worker, and partition maintenance.
- `GRAFANA_TOKENS`: Backend Grafana bearer-token catalog.

The environment owns the KSA-to-GSA bindings and IAM roles. Keep the expected
responsibilities separate:

- `sre-agent-backend`: Cloud SQL connectivity for Backend and Backend migration,
  together with the database privileges represented by `DATABASE_URL`.
- `sre-agent-outbox`: publish access to the RCA Pub/Sub topic. Its database
  access still comes from `DATABASE_URL` and the environment's database/network
  controls.
- `sre-agent-rca-worker`: subscribe access to the RCA subscription and only the
  model/MCP access approved for the production integration. Because the Worker
  migration uses this KSA, the environment must also permit that one-shot Job to
  reach the database represented by `DATABASE_URL`.

## Ordered release

Run these commands from the root of the materialized release bundle. The order
is binding: stop immediately if any command fails. In particular, do not create
the Worker migration until the Backend migration is complete, and do not apply
the full base until both migrations are complete.

```bash
kubectl apply -f deploy/k8s/base/serviceaccounts.yaml
BACKEND_JOB=$(kubectl create -f deploy/k8s/jobs/backend-migration-job.yaml -o jsonpath='{.metadata.name}')
kubectl wait --for=condition=complete --timeout=15m "job/${BACKEND_JOB}"
WORKER_JOB=$(kubectl create -f deploy/k8s/jobs/worker-migration-job.yaml -o jsonpath='{.metadata.name}')
kubectl wait --for=condition=complete --timeout=15m "job/${WORKER_JOB}"
kubectl apply -k deploy/k8s/base
kubectl rollout status deployment/sre-agent-backend --timeout=5m
kubectl rollout status deployment/sre-agent-frontend --timeout=5m
kubectl rollout status deployment/sre-agent-outbox --timeout=5m
kubectl rollout status deployment/sre-agent-rca-worker --timeout=5m
```

Each migration template uses `generateName`, so every release creates a distinct
immutable Job record. Kubernetes retains completed Jobs for 24 hours. If a Job
fails, inspect it before retrying with a newly created Job:

```bash
kubectl describe "job/${BACKEND_JOB}"
kubectl logs "job/${BACKEND_JOB}"
kubectl describe "job/${WORKER_JOB}"
kubectl logs "job/${WORKER_JOB}"
```

## Routing and production limitations

The external Gateway must route `/api` to the `sre-agent-backend` Service on
port 8000 and frontend traffic to `sre-agent-frontend` on port 8080. Gateway,
DNS, TLS, and load-balancer configuration remain outside this portable base.

Operator API authentication is not implemented for production. With
`APP_ENVIRONMENT=production`, Operator API requests continue to fail closed with
HTTP 503; never set the environment to `local` to bypass that control.

## Rollback

An application rollback may restore the previous immutable image digests and
manifests, followed by the same rollout-status checks. It must not automatically
downgrade the database: schema downgrades can destroy data or make the two
migration streams disagree. After a migration has run, use a reviewed
forward-fix migration or a separately approved database recovery procedure.
