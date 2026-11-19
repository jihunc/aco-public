FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS build
RUN pip install --no-cache-dir uv==0.13.8
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 src ./src
COPY --chown=10001:10001 alembic ./alembic
COPY --chown=10001:10001 alembic.ini pyproject.toml uv.lock ./
# Trusted build metadata only; blank keeps fresh fake-only builds usable.
# Explicit live startup requires a reviewed clean commit and matching shipped bytes.
ARG ACO_SOURCE_COMMIT=""
RUN PYTHONDONTWRITEBYTECODE=1 /app/.venv/bin/python -m aco.integrations.provider_smoke.source --build-stamp "$ACO_SOURCE_COMMIT"
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "aco.cli", "api"]
