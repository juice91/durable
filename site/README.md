# The website

The landing page and documentation published with GitHub Pages: an Astro site with the Starlight documentation theme.

- Documentation is written in the repository's `docs/` folder. `scripts/nav.mjs` lists what is published and where.
- `scripts/sync-docs.mjs` generates the pages from `docs/`, and the API reference from the API's OpenAPI schema.
- The landing page is `src/pages/index.astro`. Studio screenshots are in `docs/assets/studio`.
- No analytics, and nothing is loaded from another origin; the link check enforces it.

This folder is its own pnpm project, outside the root workspace.

```sh
pnpm install --frozen-lockfile
pnpm dev                          # preview (SITE_OPENAPI=skip leaves out the API reference)
bash build-pages.sh               # what CI and the Pages workflow run
```

To move to a custom domain, set it in the repository's Pages settings and run the Pages workflow again; nothing else
changes.

`SITE_URL` and `SITE_BASE` say where the site is served (default `https://juice91.github.io` and `/durable`). The Pages
workflow passes both from the repository's settings, so a custom domain needs no change here.

The Studio screenshots were taken from the Compose quick start with the scripted `test:*` models, at 1360×850 and
2× scale, once in each colour scheme, then resized to 1600 pixels wide.
