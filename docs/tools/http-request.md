# `http.request`

`http.request` sends one HTTPS request and returns the answer. It is the platform's general HTTP tool: a
workflow tool step or an agent can call a JSON API with a credential. (The `fetch` MCP server is GET-only and
Playwright is a browser; neither can POST JSON with a token.)

Grant it to an agent like any built-in tool (`tools: ["http.request"]`) or use it as a workflow tool step
(`tool_id: "http.request"`). It is in the deployment's `EAP_ALLOWED_TOOLS` and the Architect offers it in
`architect.list_approved_tools`.

## Arguments

| name | type | |
| --- | --- | --- |
| `method` | `GET` `POST` `PUT` `PATCH` `DELETE` | required |
| `url` | string | required; `https://` only. Anything else answers `url_not_https` |
| `headers` | object of strings | optional |
| `query` | object of strings | optional; added to the URL's query |
| `json` | any JSON | optional; sent as `application/json`. A `null` is the same as leaving it out |
| `body` | string | optional; sent as is. `json` and `body` together answer `body_conflict` |
| `timeout_s` | integer 1 to 30 | default 20; bounds the whole request |
| `max_response_bytes` | integer 1 to 262144 | default 65536 |

## Result

```json
{ "status": 200, "ok": true, "headers": {"content-type": "application/json"}, "json": {"…": "…"}, "elapsed_ms": 123 }
```

- `ok` is true for a 2xx status. **A non-2xx status is a result, not an error**: the caller sees `ok: false`
  and the body, and decides.
- `headers` holds only `content-type`, `content-length`, `location`, `retry-after`, `x-request-id` and
  `ratelimit-*`.
- `json` is the parsed body when the content type is JSON (`application/json` or `+json`) and the body fits in
  `max_response_bytes`. Otherwise `text` holds the body as UTF-8; a longer body is cut at `max_response_bytes`
  and `truncated: true` is added.
- A redirect (3xx) is returned as it is, with its `location`; it is never followed.

A transport failure is the platform's error object, `{"error": {"code": "...", "message": "..."}}` (in a
workflow tool step it fails the run with that code). The messages are fixed texts: never the URL, a header or
an exception text.

| code | when |
| --- | --- |
| `url_not_https`, `body_conflict` | the arguments |
| `outbound_blocked` | the outbound policy refused the host (see below) |
| `http_timeout` | `timeout_s` passed |
| `http_connect_failed` | the connection or TLS handshake failed |
| `secret_not_found` | a `secret:<name>` has no stored value for the tenant; **nothing was sent** |
| `secret_unreadable` | a stored secret could not be decrypted; nothing was sent |
| `response_too_large` | a JSON body is larger than `max_response_bytes` (a text body is truncated instead) |

## Secrets

A credential is never written into an agent or a workflow. Store it in **Settings → Secrets** (a masked
input), or let an agent ask for it with `secrets.request`, and write the reference `secret:<name>`:

- a string that is exactly `secret:<name>` anywhere in `headers`, `query`, `json` or `body` is replaced by the
  stored value;
- in `headers` only, a value may also contain one: `"Authorization": "Bearer secret:<name>"`.

The replacement happens inside the call, right before the request is sent. Recorded arguments, events, spans,
logs, step outputs and the run row keep the reference only. The answer (`json`, `text`, the kept headers) is
redacted with the secret's value and its obvious encodings (URL, base64, hex), so a server that echoes the
request back does not leak it. The redaction is for accidental echoes; it does not stop a server that already
holds the secret from returning it in some reversible form.

## Gating

- **In an agent run**, a call whose arguments contain at least one secret reference waits for a person's
  approval, whatever the spec's `approvals` says (and without needing an `approvals.tools` entry). The approval
  card shows the method, host and path (never the query) and the secret **names**, for example
  `POST bsky.example/xrpc/com.atproto.repo.createRecord using secret pwd-main`, plus the arguments, which hold
  references, never values. A call without a reference does not wait.
- **In a workflow tool step**, there is no approval: the person who published the workflow reviewed the URL
  and the references in the spec.

## Outbound policy

The request goes through the tenant outbound policy (the same guard as MCP servers): `https` only, no
credentials in the URL, the host must resolve to public addresses only (private, loopback, link-local, cloud
metadata, CGNAT and reserved ranges are refused, IPv4 wrapped in IPv6 included), checked again when the
connection is made, and proxies from the environment are ignored. Redirects are not followed.

## Notes

- The tool is declared `side_effect`: a workflow tool step reserves the call, so a worker that dies after the
  request was sent ends the run `needs_attention` instead of sending it twice.
- Compose a header or a request body from step outputs in a workflow with `{"$template": ...}`; see
  [Bluesky post](../workflows/bluesky-post.md).
