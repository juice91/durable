# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities through GitHub's private vulnerability reporting system for this repository. Navigate to the repository's **Security** tab and select **Report a vulnerability** to open a security advisory.

Alternatively, you may use GitHub's security advisories feature to submit a private report.

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
