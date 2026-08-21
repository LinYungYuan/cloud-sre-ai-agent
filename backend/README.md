# SRE Agent Backend

The backend service for the Observability RCA platform.

## Grafana ingestion runtime

The ASGI entry point is `sre_agent.api.main:app`. Creating or importing the app
does not read configuration, credentials, or connect to PostgreSQL. Its lifespan
validates configuration, loads the enabled Grafana source/classification catalog,
and creates the SQLAlchemy resources; invalid configuration or database/catalog
drift fails startup.

Required variables for this phase include the existing platform settings plus:

```sh
export DATABASE_URL='postgresql+asyncpg://app:password@db:5432/sre_agent'
export GRAFANA_TOKENS='{"50000000-0000-0000-0000-000000000001":{"current-2026-08":"opaque-current-token","previous-2026-07":"opaque-previous-token"}}'
export PUBSUB_PROJECT_ID='project-id'
export RCA_TOPIC_ID='rca-jobs'
export APP_ENVIRONMENT='production'
export MODEL_NAME='model-name'
export METRICS_MCP_URL='https://gateway.example/metrics/mcp'
export TRACE_MCP_URL='https://gateway.example/traces/mcp'
export LOG_MCP_URL='https://gateway.example/logs/mcp'
```

`GRAFANA_TOKENS` is JSON with this exact hierarchy: source UUID → non-secret
token ID → opaque bearer credential. Token IDs use 1–128 ASCII letters, digits,
`.`, `_`, `:`, or `-`; credentials must be non-empty ASCII without whitespace.
Rotation is done by temporarily configuring current and previous token IDs. Only
the matching token ID is persisted; token values are `SecretStr` values and must
never be logged. These are opaque bearer tokens, not JWTs.

Run the ASGI app with an installed ASGI server, for example:

```sh
uv run uvicorn sre_agent.api.main:app
```

## Production container image

Build the production image from the repository root:

```sh
docker build -t sre-agent-backend:gke-plan backend
```

The image runs as UID/GID `65532`, listens on port `8000`, and starts the ASGI
application with Uvicorn. It includes the installed worker console scripts plus
`alembic.ini` and `migrations/`, so the same image can be used by the API,
outbox worker, migration, and partition-maintenance workloads.

Maintain a current-plus-two-month partition runway independently from the app:

```sh
uv run sre-agent-ensure-partitions
# equivalent:
uv run python -m sre_agent.workers.partition_worker
```

The partition command reads `DATABASE_URL` only when invoked, returns nonzero on
failure or catalog drift, and does not provision or schedule infrastructure.
