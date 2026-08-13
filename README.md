# SRE Agent Platform

This repository contains the backend service, Angular operator frontend, and API
contracts for the SRE Agent platform. Infrastructure provisioning is outside this
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

## Contracts

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

After both projects have been set up, run the full repository gate from the root:

```bash
make check
```

It validates contracts, runs backend tests plus Ruff and Pyright, then runs the
Angular tests and production build.
