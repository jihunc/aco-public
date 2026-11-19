FROM node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e AS build
RUN npm install -g pnpm@10.30.3
WORKDIR /app
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY web/package.json web/package.json
RUN pnpm install --frozen-lockfile
COPY web ./web
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm --filter @aco/web build

FROM node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/web/.next/standalone ./
COPY --from=build --chown=10001:10001 /app/web/.next/static ./web/.next/static
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
USER 10001:10001
EXPOSE 3000
CMD ["node", "web/server.js"]
