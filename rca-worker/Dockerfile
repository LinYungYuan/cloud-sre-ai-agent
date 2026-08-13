FROM python:3.12-slim AS runtime

RUN pip install --no-cache-dir uv==0.8.15
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src

ENV PATH="/app/.venv/bin:$PATH"
USER 65532:65532
CMD ["python", "-c", "import sre_rca_worker; print(sre_rca_worker.__version__)"]
