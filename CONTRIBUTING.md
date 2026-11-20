# Contributing to Durable

Thank you for your interest in contributing to Durable! This document covers how to set up the development environment, run tests, and contribute code.

## Table of Contents

- [Setup](#setup)
- [Running Tests and Checks](#running-tests-and-checks)
- [Contributor License Agreement](#contributor-license-agreement)
- [Branch and Commit Conventions](#branch-and-commit-conventions)
- [Pull Request Process](#pull-request-process)

## Setup

### Prerequisites

- Python 3.13
- Node.js 22
- pnpm 9.15.9
- Docker (for local development database)

### Development Environment

1. **Python dependencies**

   ```bash
   uv sync --all-packages --frozen
   ```

2. **Node dependencies**

   ```bash
   pnpm install --frozen-lockfile
   ```

3. **Local Postgres (Development)**

   The development database runs via Docker Compose:

   ```bash
   docker compose -f infrastructure/docker/compose.yaml up -d
   ```

   See `infrastructure/docker/` for configuration details.

## Running Tests and Checks

### CI Checks

The CI pipeline runs the following commands. Run them locally before pushing:

#### Python checks

```bash
uv run lint-imports
```

#### Python tests (DB-free)

```bash
uv run pytest -p no:eap_db -q packages/shared-schemas/tests
uv run pytest -p no:eap_db -q packages/agent-runtime/runtime_tests
uv run pytest -p no:eap_db -q packages/kb-sources/kb_sources_tests
uv run pytest -p no:eap_db -q packages/knowledge/knowledge_tests
uv run pytest -p no:eap_db -q packages/observability/tests
uv run pytest -p no:eap_db -q packages/pricing/pricing_tests
uv run pytest -p no:eap_db -q packages/processors/processors_tests
uv run pytest -p no:eap_db -q packages/secrets/secrets_tests
```

Run a specific test path:

```bash
uv run pytest -p no:eap_db -q <path/to/tests>
```

#### Studio (frontend) checks

```bash
pnpm -C apps/studio lint
pnpm -C apps/studio exec tsc --noEmit
pnpm -C apps/studio test
```

### Selecting Affected Tests

- **Pull requests** run only the tests affected by the change (see `scripts/ci/affected.py` and `scripts/ci/select-tests.sh`).
- **Main branch** and manual CI runs execute every test suite.

To manually determine affected tests:

```bash
# Python affected tests
uv run python scripts/ci/affected.py

# Studio affected tests
bash scripts/ci/select-tests.sh studio
```

## Contributor License Agreement

Every contributor must sign the [CLA](CLA.md) before a pull request can be merged.

1. Open your first pull request. The CLA bot comments on it with a link to the agreement.
2. Reply on the pull request with exactly this sentence: `I have read the CLA Document and I hereby sign the CLA`
3. The CLA check turns green. (Comment `recheck` to re-run it if it does not update.)

One signature covers all of your future pull requests.

## Branch and Commit Conventions

### Branch Naming

- Use feature-branch naming: `feature/<description>`, `fix/<description>`, `docs/<description>`, etc.
- Keep branches focused and small.

### Commit Messages

We use [conventional commits](https://www.conventionalcommits.org/):

- `feat:` for new features
- `fix:` for bug fixes
- `docs:` for documentation changes
- `refactor:` for code restructuring
- `test:` for test additions/changes
- `chore:` for maintenance tasks

Example:

```
feat(api): add user authentication endpoint
fix(worker): handle timeout edge case in task processing
```

### Pull Request Requirements

- **Small, focused PRs**: one change per PR when possible.
- **Tests**: every change must include tests.
- **Documentation**: update docs if the change affects public behavior.
- **No secrets**: never commit credentials or sensitive data.

## Code Review

- All PRs must pass CI checks.
- At least one maintainer approval is required before merging.
- Squash-merge is used on `main`.

## Questions?

Open an issue for questions about contributing.
