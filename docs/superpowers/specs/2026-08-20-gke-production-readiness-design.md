# GKE Production Readiness Design

Date: 2026-08-20
Status: Approved in chat; awaiting written-spec review

## Objective

Prepare the SRE Agent Backend, Angular Frontend, RCA Worker, outbox publisher,
database migrations, and partition maintenance workloads for deployment on
Google Kubernetes Engine.

This work produces production container images and a Kustomize base for the
application workloads. It deliberately does not provision Google Cloud
infrastructure. Cloud SQL, Pub/Sub, Workload Identity bindings, secrets,
Gateway, DNS, and TLS remain external deployment inputs that can be managed by
Terraform later.

## Scope

The implementation covers:

- A production Backend image used by the API, outbox publisher, Backend
  migration Job, and partition-maintenance CronJob.
- A production Frontend image that serves the compiled Angular application.
- A production RCA Worker image used by the Worker Deployment and Worker
  migration Job.
- Backend liveness and database-aware readiness endpoints.
- A reduced, purpose-specific configuration model for the outbox publisher.
- Explicit Worker configuration for Pub/Sub resource auto-creation and a
  per-Pod Worker identity.
- Kubernetes Deployments, Services, ConfigMap, ServiceAccounts, CronJob, and
  ordered migration Job templates.
- Deployment and verification documentation.

The implementation excludes:

- Cloud SQL and Pub/Sub Terraform resources.
- Creation or population of Kubernetes Secrets.
- Gateway, Ingress, DNS, certificates, domains, and external load balancers.
- Operator API authentication. In non-local environments the Operator API
  continues to fail closed with HTTP 503.
- Container Registry and CI/CD pipeline implementation.
- RCA Worker horizontal scaling and Pub/Sub-backlog HPA.
- Real MCP endpoints, capability manifests, model credentials, or production
  evidence access.

## Architecture

The deployed data flow is:

```text
Frontend Service ----> Backend Service ----> Cloud SQL
                           |
                           v
                    outbox_events table
                           |
                           v
                  Outbox Deployment ----> Pub/Sub ----> RCA Worker Deployment
                                                            |
                                                            v
                                                     Cloud SQL / MCP / model
```

The Frontend and Backend are the only workloads with Kubernetes Services. The
outbox publisher and RCA Worker are background Deployments and do not accept
inbound application traffic.

One Backend image is reused with different commands for the API, outbox
publisher, Backend migration, and partition maintenance. One RCA Worker image
is reused for the Worker and Worker migration. This prevents release drift
between runtime code and schema-management code.

## Component Design

### Backend

The Backend receives a multi-stage production Dockerfile. Dependency
installation uses the checked-in `uv.lock`; the runtime runs as a non-root user
and starts Uvicorn on port 8000. Uvicorn becomes an explicit locked production
dependency instead of an undeclared local tool.

Two unauthenticated operational endpoints are added outside `/api/v1` and the
Grafana webhook namespace:

- `GET /health/live` returns success when the ASGI process can serve requests.
  It does not query PostgreSQL, preventing temporary database failures from
  causing a restart loop.
- `GET /health/ready` performs a bounded `SELECT 1` through a dedicated health
  dependency. It returns success only after application startup has completed
  and PostgreSQL is reachable.

Health responses contain no configuration, credentials, catalog content, or
exception text. Readiness failures return a generic 503 response.

Production Operator authentication is unchanged. When `APP_ENVIRONMENT` is not
`local`, the existing unavailable identity provider continues returning 503.
The deployment must not set `APP_ENVIRONMENT=local` as an authentication
bypass.

### Outbox publisher

The outbox publisher is a required runtime bridge between the transactional
database and Pub/Sub. Backend transactions persist an event in
`outbox_events`; the publisher claims pending rows and publishes them. Without
this workload, the RCA Worker receives no jobs created by the Backend.

The publisher receives a dedicated `OutboxSettings` model containing only:

- `DATABASE_URL`
- `PUBSUB_PROJECT_ID`
- `RCA_TOPIC_ID`

It no longer requires Grafana credentials, model selection, or MCP endpoints.
The publisher runs from the Backend image as a separate Deployment with no
Service.

### Frontend

The Frontend image uses Node.js 24 in the build stage and an unprivileged Nginx
runtime listening on port 8080. Nginx provides:

- Static Angular assets.
- SPA fallback to `index.html` for client-side routes.
- `/healthz` for Kubernetes probes.
- Appropriate cache separation: `index.html` and `config.json` are not cached
  as immutable assets, while hashed Angular assets may be cached long-term.

The image contains a safe default `config.json`. Kubernetes mounts the
environment-specific `config.json` from a ConfigMap, allowing `apiBaseUrl`,
locale, and time zone changes without rebuilding the image. API proxying is not
performed by Nginx in the production image; the future Gateway must route the
configured API path to the Backend Service.

### RCA Worker

The RCA Worker Dockerfile default command starts `sre-agent-rca-worker` instead
of printing the package version and exiting. The container runs as a non-root
user.

Worker settings add:

- `PUBSUB_AUTO_CREATE`, defaulting to `false`.
- `WORKER_ID`, defaulting to the container hostname when not explicitly set.

When `PUBSUB_AUTO_CREATE=false`, the Worker constructs the configured topic and
subscription paths but does not call create APIs. This supports least-privilege
production IAM when Terraform owns Pub/Sub resources. Local development sets
`PUBSUB_AUTO_CREATE=true` to retain emulator bootstrap behavior.

The Worker Deployment starts with one replica. A unique Pod hostname is used as
the lease owner, removing the hard-coded shared identity and preparing the
claim protocol for future replicas. Horizontal scaling itself remains out of
scope and requires a separate concurrency and load test.

The Worker has no network Service and therefore no readiness probe. Fatal
startup or pull-loop errors terminate the process so the Deployment controller
can restart it. The Pod receives at least 330 seconds of termination grace to
cover the configured 300-second RCA deadline.

## Database Operations

### Migrations

Terraform provisions the Cloud SQL instance, database, application role,
networking, and availability settings. Alembic migrations create and evolve the
application schema inside that database, including tables, indexes, foreign
keys, constraints, version tables, and initial partitions.

There are two ordered migration streams:

1. The Backend migration creates and upgrades the shared core schema.
2. The RCA Worker migration verifies the required Backend revision and then
   upgrades Worker-owned lifecycle and evidence fields.

Migration manifests are separate from the long-running Kustomize base. A
release pipeline must create the Backend migration Job, wait for successful
completion, create the Worker migration Job, wait for successful completion,
and only then apply or roll out the application base. A migration failure stops
the release.

Migrations do not insert environment-specific Grafana sources, normalization
rules, users, or scope grants. Those records require a separate controlled seed
or administration process outside this design.

### Partition maintenance

Several high-volume tables use monthly PostgreSQL partitions. Migrations create
only an initial runway. The partition command maintains the current month and
the following two months so a calendar transition cannot cause inserts to fail
because no matching partition exists.

A Kubernetes CronJob runs the existing partition-maintenance command daily.
It uses `concurrencyPolicy: Forbid`, has a finite retry limit and deadline, and
retains limited successful and failed Job history for operations review.

## Kubernetes Resources

The repository gains this structure:

```text
deploy/k8s/
  README.md
  base/
    kustomization.yaml
    configmap.yaml
    serviceaccounts.yaml
    backend-deployment.yaml
    backend-service.yaml
    frontend-deployment.yaml
    frontend-service.yaml
    outbox-deployment.yaml
    worker-deployment.yaml
    partition-cronjob.yaml
  jobs/
    backend-migration-job.yaml
    worker-migration-job.yaml
```

The base defines names and references but not a namespace, domain, image
registry, Google service account annotation, or Secret data. Those values are
deployment-environment concerns and can be supplied by a future overlay or
Terraform/release tooling.

All Pods use a non-root security context, disable privilege escalation, drop
Linux capabilities, use a read-only root filesystem where compatible, and
declare CPU and memory requests and limits. Writable temporary paths are backed
by `emptyDir` volumes where required.

The ConfigMap contains only non-sensitive defaults such as:

- `APP_ENVIRONMENT=production`
- Pub/Sub project, topic, and subscription identifiers as replaceable values.
- Model name and approved HTTPS MCP endpoint placeholders.
- `PUBSUB_AUTO_CREATE=false`.
- Frontend runtime configuration.

Workloads reference individual keys in a pre-existing `sre-agent-secrets`
Secret. At minimum, the Backend and database workloads require `DATABASE_URL`,
and the Backend requires `GRAFANA_TOKENS`. Secret values are never committed.

The ServiceAccounts are Kubernetes identities only. Workload Identity bindings
and IAM roles remain external. The intended permission split is:

- Outbox ServiceAccount: Pub/Sub publisher access to the RCA topic.
- Worker ServiceAccount: Pub/Sub subscriber access to the RCA subscription and
  the model/MCP access required by the selected production integration.
- Backend and migration ServiceAccounts: Cloud SQL connectivity and the
  database privileges represented by `DATABASE_URL`.

## Rollout and Failure Handling

The release order is:

```text
Terraform-provisioned dependencies available
  -> Backend migration Job succeeds
  -> Worker migration Job succeeds
  -> ConfigMap, ServiceAccounts, Services, CronJob, and Deployments applied
  -> Backend readiness succeeds
  -> external routing can send traffic
```

Backend rolling updates rely on readiness to remove unhealthy Pods from
service endpoints. Liveness is intentionally independent of PostgreSQL.

Outbox and Worker failures preserve their existing database and Pub/Sub retry
semantics. Kubernetes restarts a process only after the process boundary exits;
the manifests do not hide fatal errors with shell retry loops.

The Worker remains at one replica. The outbox publisher also starts at one
replica; its existing `FOR UPDATE SKIP LOCKED` claim behavior leaves room for a
future scaling review without making scaling part of this release.

## Testing and Verification

Implementation follows test-driven development for behavior changes. Required
verification includes:

- Backend tests for liveness, successful readiness, and sanitized readiness
  failure.
- Unit tests proving `OutboxSettings` accepts only its required runtime inputs.
- Worker tests for production no-create behavior, local auto-create behavior,
  and hostname-derived Worker identity.
- Existing Backend, RCA Worker, contract, and Frontend test/type/lint gates.
- Production builds for all three container images.
- A Kustomize render of the application base and static manifest validation.
- Container smoke tests for Backend health, Frontend `/healthz` and SPA
  fallback, and Worker process startup behavior.
- The existing local Grafana webhook end-to-end path, using PostgreSQL and the
  Pub/Sub emulator, to confirm that outbox publishing and Worker processing are
  not regressed.

No verification step creates, mutates, or deletes Google Cloud resources.

## Acceptance Criteria

The work is complete when:

- Backend, Frontend, and RCA Worker production images build reproducibly from
  checked-in lock files.
- Each image starts its intended production process as a non-root user.
- Backend liveness and database-aware readiness behave as specified.
- Frontend serves runtime configuration, client-side routes, and health checks
  from the production image.
- Production Worker startup does not require Pub/Sub resource-creation
  permission and uses a unique Worker identity.
- Outbox runs with purpose-specific settings.
- Kustomize renders all long-running application resources and the partition
  CronJob without Secret data or infrastructure resources.
- Ordered migration Job templates and operational instructions are present.
- All required automated and smoke verification passes.
- Operator authentication, Terraform resources, external routing, and secret
  values remain explicitly outside the delivered scope.
