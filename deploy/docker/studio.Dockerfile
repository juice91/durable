# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim AS base
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
RUN corepack enable && corepack prepare pnpm@9.15.9 --activate
WORKDIR /app

FROM base AS deps
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/studio/package.json apps/studio/package.json
RUN pnpm install --frozen-lockfile --filter @eap/studio...

FROM base AS builder
ARG NEXT_PUBLIC_EAP_API_URL=http://127.0.0.1:18000
ARG NEXT_PUBLIC_EAP_DEV_TOKEN="Bearer dev:studio:operator"
ENV NEXT_PUBLIC_EAP_API_URL=$NEXT_PUBLIC_EAP_API_URL
ENV NEXT_PUBLIC_EAP_DEV_TOKEN=$NEXT_PUBLIC_EAP_DEV_TOKEN
COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app/apps/studio/node_modules ./apps/studio/node_modules
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/studio ./apps/studio
RUN pnpm --filter @eap/studio build

FROM node:22-bookworm-slim AS runtime
ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
RUN groupadd --system --gid 10001 studio \
    && useradd --system --uid 10001 --gid studio --home-dir /app --shell /usr/sbin/nologin studio
COPY --from=builder --chown=studio:studio /app/apps/studio/.next/standalone ./
COPY --from=builder --chown=studio:studio /app/apps/studio/.next/static ./apps/studio/.next/static
USER 10001:10001
EXPOSE 3000
CMD ["node", "apps/studio/server.js"]
