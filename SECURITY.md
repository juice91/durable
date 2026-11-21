# Security Policy

## Reporting a Vulnerability

Please do not open a public issue for security problems.

Report it privately through GitHub: [Report a vulnerability](https://github.com/juice91/durable/security/advisories/new) (Security tab → Advisories → Report a vulnerability). Only the maintainer can see the report.

If you cannot use GitHub, say so in a minimal public issue without details and the maintainer will contact you.

## Scope

The following components are in scope:

- Core API (`apps/api/`)
- Worker service (`apps/worker/`)
- Studio frontend (`apps/studio/`)
- Shared packages (`packages/`)
- Deployment configurations (`deploy/`)

Documentation, examples, and infrastructure code (`infrastructure/`, `scripts/`) are generally out of scope unless they directly expose user-controllable input.

## Automated checks

Every pull request and every push to `main` runs these scans, and a weekly scheduled run repeats them:

- **Secrets**: gitleaks over the pull request's commits (the full history on `main` and on the schedule), plus GitHub secret scanning and push protection.
- **Dependencies**: osv-scanner on `uv.lock` and `pnpm-lock.yaml`. A vulnerability rated high or critical that has a fixed version fails the check; the rest is reported in code scanning.
- **Static analysis**: Semgrep (Python, TypeScript, Dockerfile and GitHub Actions rules) and CodeQL (`security-extended`, Python and JavaScript/TypeScript).
- **Containers**: Trivy on the Dockerfiles and Kubernetes manifests, and on the built `python` and `studio` images (a critical vulnerability with a fix fails the check). A CycloneDX SBOM for each image is attached to the run.

Dependency fixes are made in the upstream project and arrive here with the next sync, so a dependency finding on `main` can stay open until then.

## Response Expectations

- You will receive an acknowledgment of your report within **7 days**.
- We will investigate the vulnerability and provide a preliminary assessment.
- If the report is accepted, we will notify you when a fix is released.
- If the report is declined, we will provide a reason.

## Public Disclosure

We follow a coordinated disclosure process. We will coordinate with you on the timing of a public disclosure after a fix has been released.
