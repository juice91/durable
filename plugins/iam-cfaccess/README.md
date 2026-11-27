# eap-iam-cfaccess

**Legacy, single-tenant only.** Not in the deployed identity chain. Access has no tenant concept, so every Access user
becomes a user of one tenant; the plug-in refuses to start when `EAP_ZITADEL_ORG_TENANT_MAP` names another tenant (or
cannot be read) or when `EAP_TENANT_REGISTRY=enforce`. Removing it: `docs/iam.md`, "The Cloudflare Access JWT".

Identity plug-in (entry point `eap.iam.identity` / `cfaccess`): the Cloudflare Access JWT, design section 1.3 of
an internal design note.

Cloudflare Access puts a signed JWT in `Cf-Access-Jwt-Assertion` on every request it lets through. On every request the
provider checks, in order: the header is present and at most 8 KiB; the JOSE header says `RS256` and carries a `kid`;
the key comes from the team JWKS (`https://<team>/cdn-cgi/access/certs`), cached in process, refreshed on an unknown
`kid` at most once per `EAP_CF_ACCESS_JWKS_MIN_REFRESH_SECONDS` (default 30), and kept for a bounded grace period when
Cloudflare is unreachable; the signature; `iss` equals `https://<team>`; `aud` contains `EAP_CF_ACCESS_AUD`;
`exp` and `nbf` (30 s leeway); a syntactically valid `email` claim. The user subject is the lower-cased e-mail.

A request with no such header gets `None` (the next provider in `EAP_IAM_IDENTITY` decides); a request that presents one
and fails any check gets a 401 and is not retried by a weaker provider. `Cf-Access-Authenticated-User-Email` and every
other plain header are ignored. A service-token JWT (no `email`) is refused.

Configuration (all three required when the plug-in is listed; a half-configured plug-in refuses to start):

| Variable | Meaning |
|---|---|
| `EAP_CF_ACCESS_TEAM_DOMAIN` | `<team>.cloudflareaccess.com` |
| `EAP_CF_ACCESS_AUD` | the Access application AUD tag; comma-separate several to overlap during a rotation |
| `EAP_CF_ACCESS_TENANT` | the one tenant every Access user belongs to (Access has no tenant concept) |

Nothing here logs the token; log lines carry the subject and the `kid` only.
