# SRE Agent Platform

This monorepo contains three independently buildable and deployable packages for
the SRE Agent platform:

- `backend/`: FastAPI webhook and Operator REST API.
- `rca-worker/`: Pub/Sub consumer, ADK/MCP orchestration, evidence, and RCA reports.
- `frontend/`: Angular operator interface in Traditional Chinese.

`contracts/` is not a fourth deployable service. It contains versioned OpenAPI,
JSON Schema, examples, database ownership metadata, and compatibility tests used
by the three packages. Packages may consume these published formats but must not
import one another's source code. Infrastructure provisioning is outside this
repository.

## Prerequisites

- Python 3.11 or later and [uv](https://docs.astral.sh/uv/)
- Node.js `>=24.15.0 <25` and npm 11

## Backend setup

Set up the backend from its project directory. The cache location keeps uv's
downloaded artifacts inside this repository instead of a user-home cache.

```bash
cd backend
UV_CACHE_DIR="$PWD/.uv-cache" uv sync --all-groups
```

Run backend tests and static analysis independently:

```bash
cd backend
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check src tests
UV_CACHE_DIR="$PWD/.uv-cache" uv run pyright src
```

From the repository root, the equivalent command is:

```bash
make test-backend
```

## Frontend setup

Install the Angular 22 application dependencies, then run its test suite or a
production build:

```bash
cd frontend
npm ci
npm test -- --watch=false
CI=1 NG_BUILD_MAX_WORKERS=1 npm run build
```

`make test-frontend` uses the current `PATH` for Node and npm. If Node is in a
non-default location, prepend its bin directory without changing the Makefile:

```bash
NODE_BIN_DIR=/path/to/node/bin make test-frontend
```

The frontend retrieves data only through the REST API. Refresh the page manually
to retrieve the latest data.

## RCA Worker setup

The RCA Worker is a separate Python project with its own dependencies, lock file,
tests, migrations, Dockerfile, startup command, image, and release. It must not
import `backend/src`; Backend must not import `rca-worker/src`.

```bash
cd rca-worker
UV_CACHE_DIR="$PWD/.uv-cache" uv sync --all-groups
UV_CACHE_DIR="$PWD/.uv-cache" uv run pytest
UV_CACHE_DIR="$PWD/.uv-cache" uv run ruff check src tests
UV_CACHE_DIR="$PWD/.uv-cache" uv run pyright src
```

Worker migrations use `alembic_version_rca_worker` and are applied only after
the Backend migrations have reached their required revision.

## Contracts

Contracts prevent independently released packages from silently disagreeing on
payload fields, API shapes, or database ownership. They contain data formats and
compatibility checks only; they contain no business logic and are not deployed.

Contract compatibility tests validate the OpenAPI documents and checked-in
examples using the backend project's development dependencies:

```bash
UV_CACHE_DIR="$PWD/backend/.uv-cache" uv run --project backend pytest contracts/compatibility-tests
```

Or use:

```bash
make test-contracts
```

## Runtime configuration

The backend ASGI entry point is `sre_agent.api.main:app`. Backend settings are
validated during application lifespan startup rather than module import. See
[`backend/README.md`](backend/README.md) for the required `DATABASE_URL`, opaque
Grafana bearer-token JSON format, rotation guidance, and the independently
runnable partition-maintenance command. Invalid backend configuration fails
startup instead of turning valid webhook requests into generic 500 responses.

Backend, RCA Worker, and both Alembic migration streams use one shared Cloud SQL
PostgreSQL 18 application role. Angular never connects to PostgreSQL. The role
has application DML and migration DDL, but no superuser, role-management,
database-owner, or unrelated-schema privileges. New environments use that role
to run Backend migrations (`alembic_version_backend`) before RCA Worker
migrations (`alembic_version_rca_worker`). The legacy `0001_alert_incident_schema` revision
predates the package split; the implementation plan migrates its version-table
metadata without rerunning its DDL. Future migration ownership is defined by
`contracts/database/table-ownership.yaml` once that planned contract is added;
it is enforced by compatibility tests rather than separate database login roles.

Before Angular starts, the frontend loads `/config.json`. Deployments must serve
all of these fields:

```json
{
  "apiBaseUrl": "/api/v1",
  "locale": "zh-TW",
  "timeZone": "Asia/Taipei"
}
```

Application code should inject `RUNTIME_CONFIG` rather than hard-code an API
base URL. Local configuration files matching `.env*` are ignored; commit a
`.env.example` template when one is needed.

## Full verification

After all three packages have been set up, run the full repository gate from the root:

```bash
make check
```

It validates contracts, runs Backend and RCA Worker tests plus their independent
Ruff/Pyright gates, then runs the Angular tests and production build.
