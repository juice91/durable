# eap-iam-zitadel

Enterprise IAM plug-in: ZITADEL access tokens as the identity, the tenant's organisation as the membership source, and
the bootstrap CLI. Design sections 1.7, 5.7 and 11 of an internal design note.

Entry points: `eap.iam.identity` / `zitadel` and `eap.iam.membership` / `zitadel`. It owns no tables (ZITADEL holds the
users). It imports only `eap_iam`.

## Identity

A bearer that is shaped like a JWT is this provider's credential and is verified on every request: the JOSE header
must say `RS256` and carry a `kid`; the key comes from the instance JWKS (`<api url>/oauth/v2/keys`), cached in
process, refreshed on an unknown `kid` at most once per `EAP_ZITADEL_JWKS_MIN_REFRESH_SECONDS` (30), kept for a grace
period when ZITADEL is unreachable; `iss` equals the issuer exactly; `aud` contains the project id; `exp` and `nbf`
(30 s leeway). Any failure is a 401 and the chain stops (no fall-through to a weaker provider). A bearer that is not
JWT-shaped (`pos_` API keys, `dev:` bearers) is not this provider's and the next provider decides.

The principal: `tenant_id` from the org claim `urn:zitadel:iam:user:resourceowner:id` through
`EAP_ZITADEL_ORG_TENANT_MAP` (an unmapped or missing org is **403**); `roles` from `urn:zitadel:iam:org:project:roles`
(only `admin`, `editor`, `viewer`, and only those **granted by the token's own organisation**: the claim is
`{role: {granting org id: domain}}`, ZITADEL v4.20.5 `internal/api/oidc/client.go`, `projectRoles.Add`; a role granted
only by another organisation is ignored and logged at WARNING once per subject); `subject` is the stable `zitadel:<sub>` (an e-mail can change; policy tuples key on the subject). The e-mail is its
own attribute in `principal.extra["email"]`, taken from the `email` claim or, when the access token has none, from the
userinfo endpoint (cached 5 minutes); `sub`, `org_id` and `display_name` are there too. Rows written by
`eap-iam-cfaccess` hold the e-mail as subject: see `docs/iam.md`, "Subjects in recorded data". A token with no e-mail at all (a machine user) is
`kind="service"` with the `sub` as subject.

## Membership

`MembershipSource`: list, invite, change role and remove, through the ZITADEL Management API as a machine user (the JWT
profile grant; the key is `EAP_ZITADEL_MACHINE_KEY`). A member is a user of the tenant's org with a grant on the project.
Invite creates the human user through `POST /v2/users/human` with an e-mail code (ZITADEL needs SMTP configured to send
it; without SMTP the admin sets the user's password in the ZITADEL console) and grants the role. Where the machine user
lacks `user.write` in the organisation (asked with `POST /auth/v1/permissions/zitadel/me/_search`, cached a minute) it
only grants a role to a user who already exists, and refuses a new address with `no_user_creation`. The last admin can
never be demoted or removed. Without the machine user's key the source is not `configured` and the capabilities answer omits it
(Studio hides the Members page).

## Configuration

Not set at all (no project id and no org map) the plug-in is *dormant*: it recognises nothing, so the deploy can list
`zitadel` before the bootstrap has run. Half configured refuses to start.

| Variable | Meaning |
|---|---|
| `EAP_ZITADEL_ISSUER` | the public issuer, e.g. `https://auth.example-org.ai` (the exact `iss`) |
| `EAP_ZITADEL_PROJECT_ID` | the project `durable` (from the bootstrap) |
| `EAP_ZITADEL_ORG_TENANT_MAP` | `<org id>=<tenant>[,...]` or a JSON object; one org per tenant |
| `EAP_ZITADEL_API_URL` | where this process reaches ZITADEL (default: the issuer); in-cluster `http://zitadel.iam.svc.cluster.local:8080`, the `Host` header is always the issuer's host |
| `EAP_ZITADEL_MACHINE_KEY` | the machine user's key JSON (optional; membership only) |

## Bootstrap

```sh
python -m eap_iam_zitadel.bootstrap --key-file <machine-key.json> [--api-url http://127.0.0.1:8080] [--admin-user admin]
```

Idempotent: creates the project `durable` with roles admin, editor, viewer, the public Studio application (code +
PKCE), grants `admin` to the bootstrap admin and prints the org → tenant line and the values to add to the deploy
secrets. The client ids (and the Access client secret, below) go to stdout only. `--dry-run` reads only and prints what a
real run would change. See
[Expose Studio and the API](../../docs/expose.md) for publishing ZITADEL and Studio.

### Cloudflare Access (optional)

Only if Cloudflare Access should use ZITADEL as its identity provider:

```sh
python -m eap_iam_zitadel.bootstrap --cloudflare-access <team>.cloudflareaccess.com --key-file <machine-key.json> [--dry-run]
```

This also creates the confidential application `Cloudflare Access` (redirect
`https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`; the bare team name works too), or repairs it to match,
and prints the Cloudflare setup. The client secret is printed only when ZITADEL issues it; add
`--rotate-access-secret` for a new one.

Without `--cloudflare-access` the bootstrap neither reads nor writes that application and prints nothing about
Cloudflare. No environment variable turns it on. An installation that already has the application **keeps it**: the
bootstrap never deletes it. Pass the option again to repair it or to rotate its secret.

To remove it, in this order (otherwise people who sign in to Access through ZITADEL are locked out):

1. In Cloudflare Zero Trust, make sure every Access application that uses ZITADEL allows another identity provider and
   that a sign-in through it works; then remove ZITADEL from those applications and delete that identity provider.
2. In the ZITADEL console: Projects, `durable`, Applications, `Cloudflare Access`, delete.
3. Delete the stored client secret (`EAP_ZITADEL_ACCESS_CLIENT_SECRET`), and stop passing `--cloudflare-access`: a run
   with it creates the application again.

## Platform users and instance policy

```sh
python -m eap_iam_zitadel.bootstrap --instance-policy [--no-force-mfa] [--dry-run] --key-file <instance-owner-key.json>
python -m eap_iam_zitadel.bootstrap --platform-users [--rotate-key platform-members] [--dry-run] --key-file <...>
```

`--platform-users` creates `platform-provisioner` (instance `IAM_ORG_MANAGER`) and `platform-members`
(`ORG_USER_PERMISSION_EDITOR` on the operator organisation, no instance role) and prints each new key once.
`--instance-policy` turns self-registration, external sign-up, domain discovery and public organisation registration off
and requires a second factor.

## Tests

The token tests mint tokens with generated RSA keys behind a JWKS fake; membership and bootstrap tests run against a
stateful fake of the ZITADEL APIs (`iam_zitadel_tests/fakes.py`). `test_contract.py` runs the shared behaviour on the
fake **and** on a real ZITADEL v4.20.5 with its own Postgres, plus the facts only a real server can show (the role-claim
shape, what each machine user may do, registration refused, the invite link), when `EAP_TEST_ZITADEL_DOCKER=1`
(CI Heavy sets it; a skipped run is not a pass):

```sh
EAP_TEST_ZITADEL_DOCKER=1 uv run pytest -p no:eap_db -q plugins/iam-zitadel/iam_zitadel_tests/test_contract.py
```
