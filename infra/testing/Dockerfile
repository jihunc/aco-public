# Setup/build may download libraries. Test execution uses the resulting cached image by ID.
FROM node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e AS node
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && npm install -g pnpm@10.30.3
RUN apt-get update && apt-get install -y --no-install-recommends chromium=156.0.8078.79-1~deb12u1 iptables util-linux libatomic1 git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.13.8
WORKDIR /aco
ENV PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY web/package.json web/package.json
RUN pnpm install --frozen-lockfile
COPY --chown=10001:10001 . .
# uv creates this cache parent as root; runtime fixtures remain private to UID10001.
RUN uv sync --frozen && chown 10001:10001 /aco /aco/.tooling && chmod 0700 /aco/.tooling
# Public build identity only; blank images remain fake-only. No provider activation.
ARG ACO_SOURCE_COMMIT=""
RUN .venv/bin/python -m aco.integrations.provider_smoke.source --build-stamp "$ACO_SOURCE_COMMIT"
RUN .venv/bin/python scripts/portable_manifest.py write --root /aco --output /aco/.portable-inputs.json
ENV PATH="/aco/.venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" HOME="/tmp/aco-user" NEXT_TELEMETRY_DISABLED=1
RUN mkdir -p /tmp/aco-user && chown 10001:10001 /tmp/aco-user
ENTRYPOINT []
