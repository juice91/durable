# eap-iam-localadmin

Exactly one built-in admin account (username and password) for a deployment without an identity provider. Setup, operation,
reset and the switch to ZITADEL: [docs/admin-account.md](../../docs/admin-account.md). People who need several users or
roles use `eap-iam-zitadel`.

| Entry point | Name | What |
|---|---|---|
| `eap.iam.identity` | `localadmin` | password sign-in, first-run setup, and verification of the tokens it issued |
| `eap.iam.membership` | `localadmin` | the one member, role `admin`, read only |
| `eap.db.migrations` | `localadmin` | tables `iam_localadmin_*`, version table `alembic_version_localadmin` (`eap-db plugins upgrade`) |

Tests: `uv run pytest -q plugins/iam-localadmin` (needs the dev Postgres; without it only the settings and policy tests run).
