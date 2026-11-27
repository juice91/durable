# Contributing

Contributions are welcome. Start with the repository's
[contributing guide](https://github.com/juice91/durable/blob/main/CONTRIBUTING.md): it covers the Contributor License
Agreement, the security policy and the pull-request conventions. [AGENTS.md](../AGENTS.md) is the working reference for
people and coding agents.

## Set up

You need Python 3.13, [uv](https://docs.astral.sh/uv/), Node with pnpm 9 and Docker.

```sh
uv sync --all-packages --frozen
pnpm install --frozen-lockfile
cp .dev.env.example .dev.env     # model settings, optional
bash scripts/dev-up.sh           # Postgres, worker, API and Studio
bash scripts/dev-status.sh
```

`bash scripts/dev-down.sh` stops the app; `--all` also stops the Docker services.

## Test

| What | Command |
| --- | --- |
| Import contracts | `uv run lint-imports` |
| Python tests that need no database | `uv run pytest -p no:eap_db -q packages/pricing` |
| Python tests with Postgres | `bash scripts/dev-db.sh up`, then `uv run pytest -q apps/api` |
| Studio | `pnpm -C apps/studio test` |

Pull requests run the tests a change affects; see [CI](ci.md).

## Where code goes

The API is layered: routes speak the protocol, services hold the business logic, and only the run store writes SQL
([API layering](api-layering.md)). Import-linter contracts enforce the package boundaries, so a wrong import fails the
build.

## This website

The site you are reading is built from the repository's `docs/` folder; the landing page and build scripts are in
`site/`. Edit the Markdown, then:

```sh
cd site
pnpm install --frozen-lockfile
pnpm dev            # preview
pnpm run verify     # tests, build and link check, as CI does
```

A page appears in the navigation when it is listed in `site/scripts/nav.mjs`.
