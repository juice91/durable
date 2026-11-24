# eap-iam-zitadel

Enterprise IAM plug-in: ZITADEL access tokens as the identity, the tenant's organisation as the membership source, and
the bootstrap CLI. Design sections 1.7, 5.7 and 11 of `docs/superpowers/specs/2026-11-22-iam-design.md`.

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
(only `admin`, `editor`, `viewer`); `subject` is the stable `zitadel:<sub>` (an e-mail can change; policy tuples key on the subject). The e-mail is its
own attribute in `principal.extra["email"]`, taken from the `email` claim or, when the access token has none, from the
userinfo endpoint (cached 5 minutes); `sub`, `org_id` and `display_name` are there too. Rows written by
`eap-iam-cfaccess` hold the e-mail as subject: see `docs/iam.md`, "Subjects in recorded data". A token with no e-mail at all (a machine user) is
`kind="service"` with the `sub` as subject.

## Membership

`MembershipSource`: list, invite, change role and remove, through the ZITADEL Management API as a machine user (the JWT
profile grant; the key is `EAP_ZITADEL_MACHINE_KEY`). A member is a user of the tenant's org with a grant on the project.
Invite creates the human user through `POST /v2/users/human` with an e-mail code (ZITADEL needs SMTP configured to send
it; without SMTP the admin sets the user's password in the ZITADEL console) and grants the role. The last admin can never
be demoted or removed. Without the machine user's key the source is not `configured` and the capabilities answer omits it
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
PKCE), the confidential application for Cloudflare Access, grants `admin` to the bootstrap admin and prints the org →
tenant line and the values to add to the deploy secrets. The client ids and the Access client secret go to stdout only. See
`deploy/README.md`, "ZITADEL bootstrap and sign-in".

## Tests

The CI has no ZITADEL container: the token tests mint tokens with generated RSA keys behind a JWKS fake, and the
membership and bootstrap tests run against a stateful fake of the Management API (`iam_zitadel_tests/fakes.py`). The
Management API paths and field names are those of ZITADEL v4 and are not yet exercised against a live instance.
