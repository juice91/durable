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
# Empty = same-origin: the browser calls /api/v1/... on the origin that served Studio (your ingress or reverse proxy, or
# the Next.js /api rewrite). Build with a URL only to point Studio at a different API origin.
ARG NEXT_PUBLIC_EAP_API_URL=
# Target of the /api/:path* rewrite in next.config.ts (baked at build time).
ARG EAP_API_INTERNAL_URL=http://api.pydanticos.svc.cluster.local:8000
ENV NEXT_PUBLIC_EAP_API_URL=$NEXT_PUBLIC_EAP_API_URL
ENV EAP_API_INTERNAL_URL=$EAP_API_INTERNAL_URL
COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app/apps/studio/node_modules ./apps/studio/node_modules
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/studio ./apps/studio
# No credential is built in: on a public host people sign in (docs/iam.md), the loopback page gets its dev bearer at
# runtime (EAP_STUDIO_LOOPBACK_DEV_BEARER). CI greps the built bundle for the dev bearer.
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
