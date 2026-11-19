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
| `max_response_bytes` | integer 1 to 262144 (1048576 with a reducer) | default 65536 |
| `drop_response_paths` | array of JSON pointers, at most 16 | optional; see below |
| `response_query`, `extract_html`, `parse_feed` | object | optional, at most one; shrink the body inside the tool; see [Reducers](#reducers) |
| `body_from_url` | object | optional; the body is fetched from another URL first; see below. With `json` or `body` it answers `body_conflict` |

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

`drop_response_paths` (RFC 6901 pointers such as `/refreshJwt` or `/user/email`) removes those members from the
parsed `json` before the tool returns, so they never reach the step output, events, spans or checkpoints. It is
the way to keep tokens you do not need out of the record. A path that does not exist, or a result that is not
JSON, is left as it is. (Secret values you sent are redacted separately; this is for values the server sends.)

A transport failure is the platform's error object, `{"error": {"code": "...", "message": "..."}}` (in a
workflow tool step it fails the run with that code, unless the step sets [`on_error: continue`](#keeping-an-error-and-going-on)). The messages are fixed texts: never the URL, a header or
an exception text.

| code | when |
| --- | --- |
| `url_not_https`, `body_conflict`, `reducer_conflict`, `max_response_bytes_too_large` | the arguments |
| `outbound_blocked` | the outbound policy refused the host (see below) |
| `http_timeout` | `timeout_s` passed |
| `http_connect_failed` | the connection or TLS handshake failed |
| `secret_not_found` | a `secret:<name>` has no stored value for the tenant; **nothing was sent** |
| `secret_unreadable` | a stored secret could not be decrypted; nothing was sent |
| `response_too_large` | a JSON body is larger than `max_response_bytes` (a text body is truncated instead) |

## Reducers

A workflow step's output is capped at 32 KiB (`EAP_WORKFLOW_MAX_STEP_OUTPUT_BYTES`; a larger one fails the step
with `step_output_too_large`, see [Step output size](../workflows/step-output-size.md)). A Bluesky `getAuthorFeed`
with 30 posts is 42 to 54 KB of JSON and an article or listing page is 140 to 280 KB of HTML, so the whole body
cannot be the output. A **reducer** shrinks the body inside the tool, and only the reduced result is recorded: the
raw body is never in the output, an event or a span. Give at most one (two answer `reducer_conflict`; nothing is
sent). With a reducer `max_response_bytes` may go up to 1048576 (1 MiB); without one the cap stays 262144, and
`max_response_bytes_too_large` answers a larger value. A body larger than `max_response_bytes` answers
`response_too_large`.

| reducer | runs | the result goes to |
| --- | --- | --- |
| `response_query: {expression}` | the JMESPath engine of [`json.query`](json-query.md) over the JSON body (expression at most 500 characters) | `json` (`text` is absent) |
| `extract_html: {mode, base_url?, max_graphemes?, root_selector?, include_patterns?, exclude_patterns?, allowed_hosts?, limit?}` | [`html.extract`](html-extract.md) over the text body; `base_url` defaults to the request URL | `text` (`json` is absent) |
| `parse_feed: {max_items?, max_age_hours?, include_terms?, exclude_terms?, exclude_urls?, label?}` | [`feed.parse`](feed-parse.md) over the text body | `text` |

The output keeps `status`, `ok`, `headers` and `elapsed_ms` and adds `reduced: {kind, input_bytes}` (`kind` is the
reducer's name, `input_bytes` the size of the body that was read), so a run shows what happened. `drop_response_paths`
applies before `response_query`. `body_from_url` is unaffected.

Examples:

```json
{"method": "GET", "url": "https://bsky.social/xrpc/app.bsky.feed.getAuthorFeed", "query": {"actor": "example.bsky.social", "limit": "30"},
 "max_response_bytes": 1048576,
 "response_query": {"expression": "feed[?!reason].post.{uri: uri, text: record.text}"}}
```

```json
{"method": "GET", "url": "https://news.example.com/a/1", "max_response_bytes": 1048576,
 "extract_html": {"mode": "article", "max_graphemes": 3000}}
```

```json
{"method": "GET", "url": "https://news.example.com/rss.xml", "max_response_bytes": 1048576,
 "parse_feed": {"max_items": 10, "exclude_urls": ["https://news.example.com/already-posted"]}}
```

**A reducer failure** is that tool's own error, `{"error": {"code": "...", "message": "..."}}`, with the request's
status in a `request` field: `{"error": {...}, "request": {"status": 200}}`, so a caller can tell "the fetch worked,
the reduction did not". The codes are the tool's: `expression_invalid` or `result_too_large` (`response_query`),
`html_too_large`, `html_unparseable`, `html_too_complex`, `no_patterns`, `invalid_pattern`, `selector_invalid`,
`base_url_invalid` (`extract_html`), `feed_invalid` (`parse_feed`), plus `response_not_json` when `response_query`
meets a body that is not JSON.

**A non-2xx status** still applies the reducer when the body allows it (a JSON error body, say). When it does not,
the normal result is returned with `text` cut to 4 KiB (`truncated: true` when cut): never the full body.

## Keeping an error and going on

A workflow tool step whose result is `{"error": {"code": ...}}` fails the run with that code. For a best-effort
step, such as a link-card thumbnail upload that may answer `body_source_rejected` (an oversize or non-image file),
or one of several news sources answering `http_timeout`, set `"on_error": "continue"` on the step (`"fail"` is the
default; only tool steps take it). The error result is then kept, unchanged, as the step's output, the step's
`step_completed` event says `"outcome": "error_kept"` (with the `error_code`), and the run goes on to `next`. A branch
can route on it. An exception inside the tool is still a step failure.

```json
{"id": "thumb", "type": "tool", "tool_id": "http.request", "on_error": "continue", "next": "thumb_gate",
 "input": {"method": "POST", "url": "https://bsky.social/xrpc/com.atproto.repo.uploadBlob",
           "headers": {"Authorization": "Bearer secret:bsky-token"},
           "body_from_url": {"url": {"$ref": "steps.page.output.og_image"}, "allowed_hosts": ["cdn.example.com"]}}}
{"id": "thumb_gate", "type": "branch", "default": "post_with_card",
 "cases": [{"when": {"op": "exists", "left": {"$ref": "steps.thumb.output.error"}}, "next": "post_plain"}]}
```

A runnable example is [`best-effort-fetch.json`](../../examples/workflows/best-effort-fetch.json).

## Body from another URL

`body_from_url` sends bytes fetched from another URL as the request body. Its use is a binary upload such as
`com.atproto.repo.uploadBlob` for a link-card thumbnail (take the address from `html.extract`'s `og_image`),
without a tool for any one service.

```json
{"method": "POST", "url": "https://bsky.social/xrpc/com.atproto.repo.uploadBlob",
 "headers": {"Authorization": "Bearer secret:bsky-token"},
 "body_from_url": {"url": "https://cdn.example.com/thumb.jpg", "allowed_hosts": ["cdn.example.com"]}}
```

| name | type | |
| --- | --- | --- |
| `url` | string | required; `https://` only |
| `allowed_hosts` | array of hostnames, 1 to 20 | required; the URL's host must be one of them (case ignored) |
| `max_bytes` | integer 1 to 5000000 | default 1000000; at most this many bytes are read |

The worker fetches the URL first with a GET through the same outbound policy as the main request: public addresses
only, checked again at connect, and **redirects are not followed** (a 3xx is refused). The answer must be 2xx with a
`Content-Type` that starts with `image/`, and no longer than `max_bytes`. Those bytes are then the request body, and
`Content-Type` is the fetched one unless the caller set a `Content-Type` header. The credential headers go to the
main request only, never to the image host. The result has the same shape as any other call.

Any refusal is `{"error": {"code": "body_source_rejected", "detail": "..."}}` and **nothing is sent**: the host is not
in `allowed_hosts`, the URL is not https, the answer is a redirect, a non-2xx status, not an image or too large, or
the fetch failed (timeout, blocked address, connection). The detail is a fixed sentence; it never contains the URL.
The approval card of an agent call names the image host.

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

## What a workflow records

A workflow tool step's recorded input (the `step_started` event, spans, the run row) shows the values of sensitive
headers as `[REDACTED]`: `authorization`, `cookie`, `proxy-authorization`, `x-api-key` and any header whose name
contains `token` or `secret`, case-insensitively. The call itself sends the real value.

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
