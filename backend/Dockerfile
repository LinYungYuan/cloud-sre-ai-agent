# syntax=docker/dockerfile:1
FROM python:3.12.9-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install the locked production dependency set before application source so
# dependency layers can be reused when only the application changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

COPY migrations ./migrations
COPY alembic.ini ./alembic.ini


FROM python:3.12.9-slim-bookworm AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

COPY --from=builder --chown=65532:65532 /app/.venv /app/.venv
COPY --from=builder --chown=65532:65532 /app/src /app/src
COPY --from=builder --chown=65532:65532 /app/migrations /app/migrations
COPY --from=builder --chown=65532:65532 /app/alembic.ini /app/alembic.ini

USER 65532:65532

EXPOSE 8000

# Default: uvicorn sre_agent.api.main:app --host 0.0.0.0 --port 8000
CMD ["uvicorn", "sre_agent.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
