#!/usr/bin/env bash
# Builds everything GitHub Pages publishes into site/dist: the landing page, the documentation and, when the
# repository has docs/slides (the public repository does), the slide deck under /slides/. Then checks every link.
# Used by the CI site job and by the public repository's Pages workflow, so the two build the same thing.
#   SITE_URL, SITE_BASE   where the site is served (default https://juice91.github.io and /durable)
# Needs Node 22, pnpm 9 and uv (the API reference comes from the OpenAPI schema).
set -euo pipefail
site="$(cd "$(dirname "$0")" && pwd)"
cd "$site"
pnpm install --frozen-lockfile
pnpm test
pnpm build
if [ -d ../docs/slides ]; then
  rm -rf dist/slides
  cp -R ../docs/slides dist/slides
fi
pnpm check-links
