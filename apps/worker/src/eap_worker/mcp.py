"""MCP tools in agent runs (milestone 3, task 3.5).

A spec's `mcp_tools` ({server, tool} pairs naming the tenant's registered MCP servers) are added to
the run's DynamicToolset by `mcp_toolset_for_run`. DBOSDurability re-evaluates that factory inside
each `...get_tools` and `...call_tool` step; building the toolset does no I/O. Server rows are read
and header secrets decrypted only (a) in discovery and (b) inside a call whose side-effect key was
just reserved. A replayed step never needs the database or the platform key: a recorded get_tools
step is not re-executed, and a re-executed call step finds its key reserved or completed first.

- Discovery (`get_tools` step): connect to each referenced server over Streamable HTTP with
  FastMCP's client on the outbound policy's guarded HTTP client (URL re-checked, host re-resolved
  and re-checked at connect), list its tools and expose ONLY the listed ones, named
  `mcp_tool_name(server, tool)`. The recorded tool definitions are what replays and the call step use.
- Call (`call_tool` step): `run_side_effect(key=f"{run_id}:{tool_call_id}", external_idempotent=False)`
  around one `tools/call`. The crash point `after_tool_call` sits between the server's answer and
  `side_effects.complete`. A recovered step finds the key `reserved` and raises
  SideEffectOutcomeUnknown before loading anything, so the run ends `needs_attention` (even if the
  server was deleted or the key rotated meanwhile) and the server is not called again. A failure
  after the request may have been sent (timeout, dropped connection) is also an unknown outcome; a
  failure to connect at all is McpToolUnavailable (the run fails, nothing was sent).
- Retries only before a call: the model's arguments are checked against the tool's JSON schema
  (args validator) before call_tool runs, with at most MAX_ARG_RETRIES retry prompts. call_tool
  itself never raises ModelRetry/ValidationError, so no retry (a new tool call id, hence a new key
  and a second external call) can follow an attempted call. A server-reported tool error (isError)
  or a JSON-RPC rejection (parse error, invalid request, method not found, invalid params) is
  stored without its body and returned to the model as a final `ToolFailed` with a fixed message
  (TOOL_ERROR_MESSAGE).
- A server that is gone or disabled, unreadable secrets, a refused URL or a tool the server no
  longer has fail the run with McpToolUnavailable (stable code, no exception text).

Secrets: header values live only in McpServerConn.headers (excluded from repr) and the MCP transport.
Every decrypted value and its obvious encodings (`secret_variants`) are replaced by REDACTED in tool
results, tool descriptions and schemas before they are stored or shown to the model. Failures are
logged with `log_failure` (code and type only) and raised without their cause chain.

Threat model of response redaction. It protects against ACCIDENTAL echoes of a tenant's header
secrets by an MCP server: the plain value and credential part, common encodings (URL, base64, hex,
JSON-escaped, any letter case), base64/hex runs that decode to text containing it, contiguous and
simple split forms (across strings joined in document, values-only and keys-only order, and whole
strings that are pieces of it) and the truncation edge. It cannot stop a deliberately malicious
server, which already holds the secret, from returning it in an arbitrary reversible transform
(reversed, ROT13, one character per key among decoys, ...); that is out of scope for redaction. A
malicious server is contained instead by: tenant-scoped registration (it only ever receives that
tenant's own secrets), the outbound URL policy (checked again at connect, DNS-pinned, no redirects),
response size caps and identity-only encoding, no retries after a call, server error bodies never
stored or shown, and secrets never written to logs, spans, events or stored errors.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import bisect
import contextlib
import functools
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import anyio
import httpx2
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR
from pydantic import ValidationError
from pydantic_ai import ModelRetry, RunContext, ToolFailed
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import SchemaValidator, core_schema, to_jsonable_python
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from eap_observability import log_failure, redact as redact_text
from eap_runtime import SideEffectOutcomeUnknown, run_side_effect
from eap_schemas import McpToolRef, mcp_tool_name
from eap_secrets import SecretsError, load_keyring, mcp_header_context
from eap_secrets.outbound import OutboundBlocked, OutboundPolicy
from eap_worker.crash import crash_point

log = logging.getLogger("eap_worker.mcp")

TIMEOUT_ENV = "EAP_MCP_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_S = 60.0
MAX_RESULT_CHARS = 100_000  # stored in side_effects and the DBOS checkpoint; keep well under 2 MB
_ARGS_VALIDATOR = SchemaValidator(core_schema.dict_schema(core_schema.str_schema(), core_schema.any_schema()))


class McpToolUnavailable(Exception):
    """A spec's MCP tool cannot be used. Carries only names from the spec and a stable code."""

    def __init__(self, server: str, tool: str, code: str) -> None:
        self.server = server[:100]
        self.tool = tool[:128]
        self.code = code
        # args are the constructor arguments, so pickle (DBOS records a failed step's exception and
        # unpickles it on replay) rebuilds exactly this exception.
        super().__init__(self.server, self.tool, self.code)

    def __str__(self) -> str:
        return f"MCP tool {self.server}/{self.tool} is unavailable: {self.code}"


def timeout_s() -> float:
    """EAP_MCP_TIMEOUT_SECONDS (connect + one operation), clamped to [1, 600]; 60 by default."""
    try:
        value = float(os.environ.get(TIMEOUT_ENV, DEFAULT_TIMEOUT_S))
    except ValueError:
        value = DEFAULT_TIMEOUT_S
    return min(max(value, 1.0), 600.0)


@dataclass(frozen=True)
class McpServerConn:
    """A registered server ready to connect to: its URL and decrypted headers (never in repr)."""

    name: str
    url: str
    headers: dict[str, str] = field(repr=False)


_SERVERS = text(
    "SELECT id, name, url, enabled FROM mcp_servers WHERE tenant_id = :t AND name IN :names"
).bindparams(bindparam("names", expanding=True))
_SECRETS = text(
    "SELECT server_id, header_name, ciphertext, key_id FROM mcp_server_secrets "
    "WHERE tenant_id = :t AND server_id IN :ids"
).bindparams(bindparam("ids", expanding=True))


def load_servers(engine: Engine, tenant_id: str, refs: Sequence[McpToolRef]) -> dict[str, McpServerConn]:
    """The tenant's servers named by `refs`, with header secrets decrypted (blocking; run in a thread).

    Raises McpToolUnavailable (for the first tool of the server) when a server is not registered in
    this tenant (`server_not_found`), is disabled (`server_disabled`) or its secrets cannot be
    decrypted (`secret_unreadable`, also when EAP_SECRETS_KEY is unset or wrong)."""
    first_tool = {}
    for ref in refs:
        first_tool.setdefault(ref.server, ref.tool)
    with engine.connect() as conn:
        rows = {r.name: r for r in conn.execute(_SERVERS, {"t": tenant_id, "names": list(first_tool)})}
        for name, tool in first_tool.items():
            if name not in rows:
                raise McpToolUnavailable(name, tool, "server_not_found")
            if not rows[name].enabled:
                raise McpToolUnavailable(name, tool, "server_disabled")
        secrets = conn.execute(_SECRETS, {"t": tenant_id, "ids": [r.id for r in rows.values()]}).all()
    headers: dict[str, dict[str, str]] = {r.id: {} for r in rows.values()}
    if secrets:
        by_id = {r.id: r.name for r in rows.values()}
        name = by_id[secrets[0].server_id]
        try:
            ring = load_keyring()
            for server_id, header, ciphertext, key_id in secrets:
                name = by_id[server_id]
                context = mcp_header_context(tenant_id, server_id, header)
                headers[server_id][header] = ring.decrypt(bytes(ciphertext), key_id, context=context)
        except SecretsError as exc:
            log_failure(log, "mcp server secret could not be decrypted", exc, code="secret_unreadable",
                        tenant_id=tenant_id, server=name)
            raise McpToolUnavailable(name, first_tool[name], "secret_unreadable") from None
    return {r.name: McpServerConn(r.name, r.url, headers[r.id]) for r in rows.values()}


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    stack = [exc]
    while stack:
        cur = stack.pop(0)
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        stack.extend(e for e in (cur.__cause__, cur.__context__) if e is not None)


def failure_code(exc: BaseException) -> str:
    """A stable code for a failed connection or listing (never exception text)."""
    chain = list(_chain(exc))
    for e in chain:
        if isinstance(e, OutboundBlocked):
            return e.code
        if isinstance(e, ResponseTooLarge):
            return "response_too_large"
        if isinstance(e, UnsupportedEncoding):
            return "unsupported_encoding"
    if any(isinstance(e, TimeoutError) for e in chain):
        return "timeout"
    return "connect_failed"


def _unknown_code(exc: BaseException) -> str:
    """The log code for a failure after the request may have been sent (all: unknown outcome)."""
    for e in _chain(exc):
        if isinstance(e, ResponseTooLarge):
            return "response_too_large"
        if isinstance(e, UnsupportedEncoding):
            return "unsupported_encoding"
        if type(e).__name__ == "InputRequiredRoundsExceededError":
            return "input_required"
    return "call_outcome_unknown"


def _definite_rejection(exc: BaseException) -> bool:
    """True when the server answered the call with a JSON-RPC rejection (nothing was executed)."""
    return any(isinstance(e, MCPError) and e.code in _REJECTION_CODES for e in _chain(exc))


def _has_non_local_ref(schema: Any) -> bool:
    """Whether any $ref / $dynamicRef in `schema` points outside the schema document."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in ("$ref", "$dynamicRef") and not (isinstance(value, str) and value.startswith("#")):
                return True
            if _has_non_local_ref(value):
                return True
    elif isinstance(schema, list):
        return any(_has_non_local_ref(v) for v in schema)
    return False


def _describe(error: Any) -> str:
    """One validation error as `argument '<path>' is invalid: <keyword>`. A missing required
    argument is named (its name is in the schema the model is shown); no other schema value is."""
    path = "/".join(map(str, error.path))[:100] or "(arguments)"
    if error.validator == "required" and isinstance(error.instance, dict):
        missing = [str(n)[:64] for n in error.validator_value or () if n not in error.instance][:5]
        return f"argument '{path}' is invalid: required (missing: {', '.join(missing)})"
    return f"argument '{path}' is invalid: {str(error.validator)[:40]}"


def _schema_check(schema: dict[str, Any]) -> Callable[..., None]:
    """An args_validator_func: the model's arguments against the tool's JSON schema, as a bounded
    ModelRetry (a retry prompt) before anything is sent.

    The schema is the server's: it is used only with an empty `referencing.Registry` (nothing is
    ever retrieved: jsonschema would otherwise GET a remote $ref with `requests`, outside the
    outbound policy), and a schema with any non-local reference, an invalid schema, or one that
    fails while validating checks nothing. Retry text names only the argument path and the failing
    JSON-schema keyword, never jsonschema's message (it quotes schema values), and is redacted."""
    import jsonschema
    from referencing import Registry

    validator: Any = None
    if not _has_non_local_ref(schema):
        try:
            cls = jsonschema.validators.validator_for(schema)
            cls.check_schema(schema)
            validator = cls(schema, registry=Registry())
        except Exception:  # noqa: BLE001 - the server's schema is not ours to enforce if it is invalid
            validator = None

    def check(ctx: RunContext[Any], **args: Any) -> None:
        if validator is None:
            return
        try:
            errors = sorted(validator.iter_errors(args), key=lambda e: [str(p) for p in e.path])
        except Exception:  # noqa: BLE001 - e.g. an unresolvable reference: treat as an invalid schema
            return
        if errors:
            raise ModelRetry(redact_text("Invalid arguments for this tool: " + "; ".join(map(_describe, errors[:5]))))

    return check


MAX_RESPONSE_BYTES = 1024 * 1024  # per HTTP response body, JSON or SSE (the SDK's SSE event cap)


class ResponseTooLarge(Exception):
    """An MCP server's HTTP response body passed MAX_RESPONSE_BYTES; reading stopped there."""


class _CappedStream(httpx2.AsyncByteStream):
    """A response body that raises ResponseTooLarge once more than `cap` bytes arrived, so neither
    `aread()` (application/json) nor the SSE reader can buffer an unbounded body."""

    def __init__(self, inner: Any, cap: int) -> None:
        self._inner = inner
        self._cap = cap

    async def __aiter__(self) -> Any:
        total = 0
        async for chunk in self._inner:
            total += len(chunk)
            if total > self._cap:
                raise ResponseTooLarge()
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class UnsupportedEncoding(Exception):
    """An MCP server answered with a Content-Encoding other than identity although the client asked
    for identity only. Its body is never read (a compressed body could expand without bound)."""


async def _cap_response(response: httpx2.Response) -> None:
    """Response hook, before any body byte is read: refuse an encoded body, cap the rest.

    The client sends `Accept-Encoding: identity`, so raw bytes ARE decoded bytes and the cap bounds
    exactly what aread() / the SSE reader / JSON parsing see. A server that compresses anyway is
    refused here, so httpx2 never decompresses anything."""
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        raise UnsupportedEncoding()
    response.stream = _CappedStream(response.stream, MAX_RESPONSE_BYTES)


def _http_client_factory(policy: OutboundPolicy) -> Callable[..., httpx2.AsyncClient]:
    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        client = policy.httpx_client_factory(**kwargs)
        client.headers["Accept-Encoding"] = "identity"
        client.event_hooks.setdefault("response", []).append(_cap_response)
        return client

    return factory


def _client(conn: McpServerConn, policy: OutboundPolicy, timeout: float) -> Any:
    """A FastMCP client over Streamable HTTP on the policy's guarded HTTP client (not yet connected).

    Response bodies are capped (MAX_RESPONSE_BYTES). `input_required_max_rounds=0`: an
    InputRequiredResult is never answered by re-sending tools/call (each re-send would execute the
    tool again inside one reserved side effect); it raises instead, an unknown outcome."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    policy.check_url(conn.url)  # raises OutboundBlocked; the transport re-checks on every request
    transport = StreamableHttpTransport(
        conn.url, headers=dict(conn.headers), httpx_client_factory=_http_client_factory(policy)
    )
    return Client(transport, timeout=timeout, init_timeout=timeout, input_required_max_rounds=0)


@contextlib.asynccontextmanager
async def _session(conn: McpServerConn, policy: OutboundPolicy, timeout: float) -> AsyncIterator[Any]:
    """A connected client with FastMCP's own spans suppressed: they would carry a tool's error text
    (a server-controlled body) in the span status. Trace context still propagates."""
    from fastmcp.telemetry import suppress_fastmcp_telemetry

    with suppress_fastmcp_telemetry():
        async with _client(conn, policy, timeout) as client:
            yield client


REDACTED = "[REDACTED]"
TOOL_ERROR_MESSAGE = (
    "The MCP server reported that this tool call failed. Details are withheld. "
    "The call has finished; do not repeat it."
)
_MIN_FRAGMENT = 4
MAX_ARG_RETRIES = 2  # invalid-argument retries per tool; they happen before any call is attempted
# JSON-RPC errors that mean the server rejected the request without executing anything. Timeouts,
# closed connections and internal errors (-32603, possibly after a partial effect) stay unknown.
_REJECTION_CODES = frozenset({PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS})
_MAX_DECODE_RUN = 200_000
# Runs that may be base64 (std or url-safe) or hex of something that contains a secret.
_ENCODED_RUN = re.compile(r"[A-Za-z0-9+/_-]{12,}={0,2}")


class SecretVariants(list):
    """secret_variants' result: a list of forms, plus `.encoded` (the base64/hex forms) and `.hex`
    (the hex forms among them). In a whole-string piece check, base64 matches exactly (re-cased
    base64 is a different encoding) and hex case-insensitively (hex decoding ignores case)."""

    encoded: frozenset[str] = frozenset()
    hex: frozenset[str] = frozenset()


def secret_variants(values: Iterable[str]) -> SecretVariants:
    """Every string to scrub for these header values, longest first: the value, each credential
    part after an auth scheme ("Bearer <token>" -> token), a Basic credential's decoded form and its
    password, and the URL-encoded, base64 (std/url-safe, with and without padding), hex and
    JSON-escaped forms of each. Matching is case-insensitive (see `redact`), which also covers
    upper-cased copies, lower-case percent-encoding and upper-case hex."""
    values = [v for v in values if v]
    found: set[str] = set()
    encoded: set[str] = set()
    hex_forms: set[str] = set()
    for value in values:
        base = {value}
        parts = value.split()
        base.update(p for p in parts[1:] if len(p) >= _MIN_FRAGMENT)
        for part in parts[1:]:
            try:
                decoded = base64.b64decode(part, validate=True).decode("utf-8")
            except (binascii.Error, ValueError):
                continue
            base.add(decoded)
            base.update(p for p in decoded.split(":", 1)[1:] if len(p) >= _MIN_FRAGMENT)
        for item in base:
            raw = item.encode()
            for enc in (base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()):
                encoded.update({enc, enc.rstrip("=")})
            encoded.add(raw.hex())
            hex_forms.add(raw.hex())
            found.update({item, quote(item, safe=""), json.dumps(item)[1:-1]})
    encoded -= found  # a form that is also plain text (e.g. an all-hex token) stays case-insensitive
    result = SecretVariants(
        sorted((f for f in found | encoded if len(f) >= _MIN_FRAGMENT or f in values), key=len, reverse=True)
    )
    result.encoded = frozenset(encoded)
    result.hex = frozenset(hex_forms & encoded)
    return result


@functools.lru_cache(maxsize=64)
def _pattern(variants: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(v) for v in variants), re.IGNORECASE)


def _decodings(run: str) -> Iterator[str]:
    """What an encoded-looking run may decode to (base64 std/url-safe, hex), as text."""
    if len(run) > _MAX_DECODE_RUN:
        return
    body = run.rstrip("=")
    body = body[: len(body) - len(body) % 4] if len(body) % 4 == 1 else body  # a cut-off run
    padded = body + "=" * (-len(body) % 4)
    for decode in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            yield decode(padded).decode("utf-8", "ignore")
        except (binascii.Error, ValueError):
            pass
    if len(run) % 2 == 0:
        try:
            yield bytes.fromhex(run).decode("utf-8", "ignore")
        except ValueError:
            pass


def _redact_str(s: str, pattern: re.Pattern[str]) -> str:
    s = pattern.sub(REDACTED, s)

    def check(m: re.Match[str]) -> str:  # a secret inside base64/hex of a larger blob (e.g. JSON)
        return REDACTED if any(pattern.search(d) for d in _decodings(m.group(0))) else m.group(0)

    return _ENCODED_RUN.sub(check, s)


def redact(obj: Any, variants: Sequence[str]) -> Any:
    """`obj` (JSON-like) with every variant replaced by REDACTED, case-insensitively, in keys and
    values, recursively; also any base64/hex run whose decoded text contains a variant."""
    if not variants:
        return obj
    pattern = _pattern(tuple(variants))
    if isinstance(obj, str):
        return _redact_str(obj, pattern)
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        renamed = 0
        for k, v in obj.items():
            key = redact(k, variants) if isinstance(k, str) else k
            if key != k or key in out:  # a key that held a secret: a unique placeholder name
                renamed += 1
                key = f"{REDACTED}-{renamed}"
            out[key] = redact(v, variants)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, variants) for v in obj]
    return obj


def _cut_redacted(text: str, limit: int, variants: Sequence[str]) -> str:
    """`text` (longer than `limit`) cut to at most `limit` characters and redacted, with no part of a
    secret left at the cut. Bounded work: only `limit + W` characters are ever redacted.

    W is the longest scrubbed form + 4 (the hex form, 2x the secret, bounds a secret's base64/hex
    encoding inside a longer run too). The first `limit + W` characters are redacted, so every secret
    wholly inside them is gone; the only secret that can survive is one straddling that window's end,
    and its visible part lies within the last W characters, which are dropped before the final cut."""
    width = max((len(v) for v in variants), default=0) + 4 if variants else 0
    window = text[: limit + width]
    red = redact(window, variants)
    if len(text) > len(window) and width:
        red = red[: max(0, len(red) - width)]
    return red[:limit]


def _leaves_with_kind(obj: Any) -> list[tuple[str, bool]]:
    """Every string in `obj` in traversal order, with whether it is a dict key: each key followed by
    its value's strings, list items, nested. (Keys count: a secret can span a key and its value.)"""
    if isinstance(obj, str):
        return [(obj, False)]
    if isinstance(obj, dict):
        return [x for k, v in obj.items() for x in [(str(k), True), *_leaves_with_kind(v)]]
    if isinstance(obj, (list, tuple)):
        return [x for v in obj for x in _leaves_with_kind(v)]
    return []


def _string_leaves(obj: Any) -> list[str]:
    return [s for s, _ in _leaves_with_kind(obj)]


def _split_masks(leaves: list[tuple[str, bool]], pattern: re.Pattern[str]) -> dict[int, list[tuple[int, int]]]:
    """Character ranges to mask, per leaf index, for every secret match that spans two or more leaves
    when the leaves are joined, in three orders: keys and values interleaved, values only, keys only."""
    masks: dict[int, list[tuple[int, int]]] = {}
    orders = (
        list(range(len(leaves))),
        [i for i, (_, is_key) in enumerate(leaves) if not is_key],
        [i for i, (_, is_key) in enumerate(leaves) if is_key],
    )
    for indices in orders:
        if len(indices) < 2:
            continue
        starts, pos = [], 0
        for i in indices:
            starts.append(pos)
            pos += len(leaves[i][0])
        joined = "".join(leaves[i][0] for i in indices)
        for m in pattern.finditer(joined):
            first = bisect.bisect_right(starts, m.start()) - 1
            last = bisect.bisect_right(starts, m.end() - 1) - 1
            if first == last:
                continue  # inside one leaf: plain redaction handles it
            for n in range(first, last + 1):
                lo = max(m.start(), starts[n]) - starts[n]
                hi = min(m.end(), starts[n] + len(leaves[indices[n]][0])) - starts[n]
                if hi > lo:
                    masks.setdefault(indices[n], []).append((lo, hi))
    return masks


def _masked(text: str, ranges: list[tuple[int, int]]) -> str:
    out, pos = [], 0
    for lo, hi in sorted(ranges):
        if hi <= pos:
            continue
        out.append(text[pos:max(lo, pos)])
        out.append(REDACTED)
        pos = hi
    out.append(text[pos:])
    return "".join(out)


def _mask_split_secrets(content: Any, variants: Sequence[str]) -> Any:
    """`content` with every part of a secret that is split across strings (values, list items, a
    key and its value) replaced by REDACTED in each string it touches; the structure is kept."""
    if not variants:
        return content
    leaves = _leaves_with_kind(content)
    masks = _split_masks(leaves, _pattern(tuple(variants)))
    # A whole string (key or value) of 4+ characters that is a piece of a secret form is masked too,
    # whatever separates it from the other pieces. Only whole strings: no substring of a longer one.
    # Text and hex forms match case-insensitively (hex decoding ignores case); base64 forms exactly
    # (re-cased base64 is a different encoding).
    encoded = getattr(variants, "encoded", frozenset())
    hex_forms = getattr(variants, "hex", frozenset())
    folded_forms = "\0".join(v.lower() for v in variants if v not in encoded or v in hex_forms)
    base64_forms = "\0".join(encoded - hex_forms)
    longest = max(len(v) for v in variants)
    for i, (leaf, _) in enumerate(leaves):
        if _MIN_FRAGMENT <= len(leaf) <= longest and (leaf.lower() in folded_forms or leaf in base64_forms):
            masks.setdefault(i, []).append((0, len(leaf)))
    if not masks:
        return content
    counter = iter(range(len(leaves)))

    def rebuild(obj: Any) -> Any:
        if isinstance(obj, str):
            i = next(counter)
            return _masked(obj, masks[i]) if i in masks else obj
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for k, v in obj.items():
                i = next(counter)
                key = _masked(str(k), masks[i]) if i in masks else k
                if key in out:
                    key = f"{key}-{i}"
                out[key] = rebuild(v)
            return out
        if isinstance(obj, (list, tuple)):
            return [rebuild(v) for v in obj]
        return obj

    return rebuild(content)


def _result_json(result: Any, variants: Sequence[str]) -> dict[str, Any]:
    """A CallToolResult as JSON for side_effects: {"is_error", "content"}, secrets scrubbed, bounded.

    An error result keeps no body at all (content None): its text is the server's and may quote
    anything, so neither the store nor the model sees it. Otherwise structured content wins; text
    blocks become strings (one block: a bare string), other blocks (images, ...) only their type.

    A secret split across strings (text blocks, structured fields, list items, a key and its value)
    is invisible string by string, so each part of it is masked where it lies (`_mask_split_secrets`);
    the structure is kept. An oversized result is cut BEFORE redaction (bounded work; see `_cut_redacted`:
    nothing that could be part of a secret straddling the cut survives) and stored as text."""
    if result.is_error:
        return {"is_error": True, "content": None}
    if result.structured_content is not None:
        content: Any = to_jsonable_python(result.structured_content, fallback=str)
    else:
        parts: list[Any] = [
            b.text if getattr(b, "type", None) == "text" else {"type": str(getattr(b, "type", "unknown")), "omitted": True}
            for b in result.content
        ]
        content = parts[0] if len(parts) == 1 and isinstance(parts[0], str) else parts
    content = _mask_split_secrets(content, variants)  # linear in the (capped) body
    encoded = json.dumps(content, default=str)
    if len(encoded) > MAX_RESULT_CHARS:
        text = _cut_redacted(encoded, MAX_RESULT_CHARS, variants)
        return {"is_error": False, "content": {"truncated": True, "text": text}}
    return {"is_error": False, "content": redact(content, variants)}


Loader = Callable[[Sequence[McpToolRef]], dict[str, McpServerConn]]


class McpToolset(AbstractToolset[Any]):
    """Exactly the spec's MCP tools. Building it does no I/O: server rows and secrets are read by
    `load` only inside discovery and inside a call that has just reserved its side-effect key, so a
    replayed step (recorded get_tools, or a call whose key is already reserved/completed) needs
    neither the database nor the platform key. One short MCP session per listing or call."""

    def __init__(self, refs: Sequence[McpToolRef], load: Loader, timeout: float) -> None:
        self.refs = {mcp_tool_name(r.server, r.tool): r for r in refs}
        self._load = load
        self._timeout = timeout
        self._tools: dict[str, ToolsetTool[Any]] | None = None

    @property
    def id(self) -> str | None:
        return None  # always inside the run's DynamicToolset, which carries the durable id

    def __repr__(self) -> str:
        return f"McpToolset(tools={sorted(self.refs)!r})"

    def _tool(self, tool_def: ToolDefinition) -> ToolsetTool[Any]:
        # Retries only for invalid arguments, which are checked before call_tool runs (nothing sent).
        # call_tool never raises ModelRetry/ValidationError, so no retry follows an attempted call.
        return ToolsetTool(
            toolset=self,
            tool_def=tool_def,
            max_retries=MAX_ARG_RETRIES,
            args_validator=_ARGS_VALIDATOR,
            args_validator_func=_schema_check(tool_def.parameters_json_schema),
        )

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        if self._tools is None:
            self._tools = await self._discover(ctx)
        return self._tools

    async def _discover(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        servers = await asyncio.to_thread(self._load, list(self.refs.values()))
        policy = OutboundPolicy.from_env()
        listed: dict[str, dict[str, Any]] = {}
        for server, conn in servers.items():
            first = next(r.tool for r in self.refs.values() if r.server == server)
            try:
                with anyio.fail_after(self._timeout):
                    async with _session(conn, policy, self._timeout) as client:
                        listed[server] = {t.name: t for t in await client.list_tools()}
            except Exception as exc:  # noqa: BLE001 - reduced to a stable code
                code = failure_code(exc)
                log_failure(log, "mcp tool discovery failed", exc, code=code,
                            run_id=ctx.deps.run_id, tenant_id=ctx.deps.tenant_id, server=server)
                raise McpToolUnavailable(server, first, code) from None
        tools: dict[str, ToolsetTool[Any]] = {}
        for name, ref in self.refs.items():
            found = listed[ref.server].get(ref.tool)
            if found is None:
                raise McpToolUnavailable(ref.server, ref.tool, "tool_not_found")
            variants = secret_variants(servers[ref.server].headers.values())
            schema = found.input_schema if isinstance(found.input_schema, dict) else {}
            tools[name] = self._tool(ToolDefinition(
                name=name,
                description=redact(found.description, variants),
                parameters_json_schema=redact(schema, variants) or {"type": "object", "properties": {}},
                metadata={"mcp_server": ref.server, "mcp_tool": ref.tool},
            ))
        return tools

    async def get_tool_for_tool_def(self, tool_def: ToolDefinition, ctx: RunContext[Any]) -> ToolsetTool[Any]:
        """Rebuild from the definition the discovery step recorded: no listing, no I/O."""
        if tool_def.name not in self.refs:
            raise KeyError(tool_def.name)
        return self._tool(tool_def)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        ref = self.refs[name]
        if ctx.tool_call_id is None:
            raise ValueError("tool_call_id is required for an MCP tool call")
        key = f"{ctx.deps.run_id}:{ctx.tool_call_id}"
        loop = asyncio.get_running_loop()

        def call() -> dict[str, Any]:
            # Runs only for a key run_side_effect has just reserved (never for one found reserved or
            # completed), so the server row and secrets are read only when a call will really be
            # made. It runs in a thread (the store is synchronous) and drives the MCP call on the loop.
            conn = self._load([ref])[ref.server]
            result = asyncio.run_coroutine_threadsafe(self._invoke(conn, ref, tool_args, key, ctx), loop).result()
            crash_point("after_tool_call")  # the server answered; side_effects.complete has not run
            return result

        try:
            stored, _performed = await asyncio.to_thread(
                run_side_effect, ctx.deps.side_effects, key, call, external_idempotent=False
            )
        except (ModelRetry, ValidationError):  # never a retry once a call may have been attempted
            raise SideEffectOutcomeUnknown(key) from None
        if stored.get("is_error"):
            # A final result, not a retry: the model sees a fixed message, never the server's text.
            raise ToolFailed(TOOL_ERROR_MESSAGE)
        return stored.get("content")

    async def _invoke(
        self, conn: McpServerConn, ref: McpToolRef, args: dict[str, Any], key: str, ctx: RunContext[Any]
    ) -> dict[str, Any]:
        """One tools/call. Before the request can have been sent, a failure is McpToolUnavailable
        (nothing happened, the run fails); after, it is SideEffectOutcomeUnknown (needs_attention)."""
        sent = False
        result: Any = None
        stored: dict[str, Any] | None = None
        variants = secret_variants(conn.headers.values())
        try:
            with anyio.fail_after(self._timeout):
                async with _session(conn, OutboundPolicy.from_env(), self._timeout) as client:
                    sent = True
                    result = await client.call_tool_mcp(ref.tool, args)
                    stored = _result_json(result, variants)  # bounded: capped body, cut before redaction
        except Exception as exc:  # noqa: BLE001 - reduced to a stable code
            if stored is None and result is not None:
                # The answer arrived but could not be processed: whether it is usable is unknown.
                log_failure(log, "mcp tool result unusable", exc, code="result_unusable", run_id=ctx.deps.run_id,
                            tenant_id=ctx.deps.tenant_id, server=ref.server, tool=ref.tool)
                raise SideEffectOutcomeUnknown(key) from None
            if result is None and sent and _definite_rejection(exc):
                # The server refused the request itself (JSON-RPC error): nothing was executed.
                log_failure(log, "mcp tool call rejected", exc, code="call_rejected", run_id=ctx.deps.run_id,
                            tenant_id=ctx.deps.tenant_id, server=ref.server, tool=ref.tool)
                return {"is_error": True, "content": None}
            if result is None:
                code = failure_code(exc) if not sent else _unknown_code(exc)
                log_failure(log, "mcp tool call failed", exc, code=code, run_id=ctx.deps.run_id,
                            tenant_id=ctx.deps.tenant_id, server=ref.server, tool=ref.tool)
                if not sent:
                    raise McpToolUnavailable(ref.server, ref.tool, code) from None
                raise SideEffectOutcomeUnknown(key) from None
            # The answer arrived; only closing the session failed. Keep the answer.
            log_failure(log, "mcp session close failed", exc, code="close_failed", level=logging.INFO,
                        run_id=ctx.deps.run_id, server=ref.server)
        assert stored is not None
        return stored


def mcp_toolset_for_run(engine: Engine, tenant_id: str, refs: Sequence[McpToolRef]) -> McpToolset:
    """The run's MCP toolset. No I/O here: it is built inside every step that evaluates the factory."""
    return McpToolset(refs, functools.partial(load_servers, engine, tenant_id), timeout_s())


class RunToolset(AbstractToolset[Any]):
    """The run's built-in tools plus its MCP tools. Unlike CombinedToolset, rebuilding a tool from a
    recorded definition (the call step) routes by name, so it never lists the MCP server's tools."""

    def __init__(self, builtin: AbstractToolset[Any], mcp: McpToolset) -> None:
        self.builtin = builtin
        self.mcp = mcp

    @property
    def id(self) -> str | None:
        return None

    async def __aenter__(self) -> RunToolset:
        await self.builtin.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        return await self.builtin.__aexit__(*args)

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        builtin = await self.builtin.get_tools(ctx)
        mcp = await self.mcp.get_tools(ctx)
        clash = sorted(set(builtin) & set(mcp))
        if clash:  # AgentSpec validation prevents this; never let one tool shadow another
            raise ValueError(f"tool name used twice: {clash[0]}")
        return {**builtin, **mcp}

    async def get_tool_for_tool_def(self, tool_def: ToolDefinition, ctx: RunContext[Any]) -> ToolsetTool[Any]:
        if tool_def.name in self.mcp.refs:
            return await self.mcp.get_tool_for_tool_def(tool_def, ctx)
        return await self.builtin.get_tool_for_tool_def(tool_def, ctx)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        return await tool.toolset.call_tool(name, tool_args, ctx, tool)

    def apply(self, visitor: Any) -> None:
        self.builtin.apply(visitor)
        self.mcp.apply(visitor)


__all__ = ["McpServerConn", "McpToolUnavailable", "McpToolset", "RunToolset", "failure_code", "load_servers", "mcp_toolset_for_run"]
