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

## Response Expectations

- You will receive an acknowledgment of your report within **7 days**.
- We will investigate the vulnerability and provide a preliminary assessment.
- If the report is accepted, we will notify you when a fix is released.
- If the report is declined, we will provide a reason.

## Public Disclosure

We follow a coordinated disclosure process. We will coordinate with you on the timing of a public disclosure after a fix has been released.
