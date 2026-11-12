# eap-secrets

Platform-level security primitives with no web, database, agent or platform imports (enforced by
`lint-imports`), so both the API and the worker can use them.

## Encrypted secrets (`eap_secrets`)

AES-256-GCM with a platform key from the environment:

| Variable | Meaning |
|---|---|
| `EAP_SECRETS_KEY` | Current key: base64 of exactly 32 random bytes. New secrets are sealed with it. |
| `EAP_SECRETS_OLD_KEYS` | Optional, comma-separated earlier keys, used only to decrypt older secrets. |

Generate a key (never commit it):

```
python -c 'import base64,os; print(base64.b64encode(os.urandom(32)).decode())'
```

Each ciphertext is stored with its `key_id` (a SHA-256 fingerprint of the key, not the key), so a
rotation is: new key in `EAP_SECRETS_KEY`, previous key appended to `EAP_SECRETS_OLD_KEYS`. Callers
pass a `context` (for MCP headers: `tenant/server/header`) as associated data, so a ciphertext moved
to another row or tenant does not decrypt. `load_keyring()` raises `SecretsKeyMissing` (with the
generation command) when the key is unset and `SecretsKeyInvalid` when it is malformed; no error
message ever contains key material or plaintext.

The API validates the key at startup (malformed: startup fails; unset: a warning, and any request
that stores or uses header secrets answers 503 with the message above).

## Outbound policy (`eap_secrets.outbound`)

The SSRF guard for tenant-supplied URLs (MCP servers):

- `OutboundPolicy.check(url)` at registration: https only, no credentials in the URL, at most 2048
  characters, and every resolved address must be public (private, loopback, link-local, multicast,
  CGNAT, unspecified, reserved and other non-global ranges are refused, for IPv4 and IPv6, including
  IPv4-mapped, 6to4 and NAT64 forms).
- `OutboundPolicy.http_client()` / `.httpx_client_factory` for the connection: re-checks the URL on
  every request and re-resolves and re-checks the host on every new connection, then connects to the
  checked address (DNS rebinding cannot swap it). Proxy environment variables are ignored and
  redirects are not followed.
- `EAP_MCP_DEV_ALLOW_HOSTS` (comma-separated exact hosts): those hosts may use http and private
  addresses. Local development and tests only.

Refusals raise `OutboundBlocked` with a stable `code` (`url_scheme_not_allowed`, `url_credentials`,
`url_too_long`, `url_invalid`, `url_host_missing`, `host_unresolvable`, `address_blocked` plus
`address_class`).

Tests: `uv run pytest -q packages/secrets`.
