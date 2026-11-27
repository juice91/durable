"""Signature presets and the verifier (design 4.3, 4.4).

Rules that hold for every preset:

- The HMAC is computed over the raw bytes with each of the trigger's secrets (at most two) against each header candidate
  (at most `MAX_CANDIDATES`), every pair is computed, and the results are combined only at the end: no early exit, so
  the time does not say which secret or candidate matched. The comparison is `hmac.compare_digest`.
- The timestamp window is checked only AFTER a signature matched (the timestamp is signed material; a stale delivery
  with a valid signature proves the sender, one with an invalid signature proves nothing).
- A dedupe key is built only from bytes the signature covers (`dedupe_key`): an unsigned id header is never trusted.
- Algorithms are an enum (sha256, sha512). MD5 and SHA-1 do not exist here; GitHub's legacy `X-Hub-Signature` is ignored.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PRESETS = ("github", "standard_webhooks", "custom")
Preset = Literal["github", "standard_webhooks", "custom"]

MAX_HEADER_BYTES = 1024
MAX_CANDIDATES = 8
MAX_SECRETS = 2
MAX_DEDUPE_KEY_CHARS = 200

REASON_MISSING_HEADER = "missing_header"
REASON_MALFORMED = "malformed_signature"
REASON_MISMATCH = "signature_mismatch"
REASON_STALE = "timestamp_stale"
REASON_SECRET_MISSING = "secret_missing"
REASON_SECRET_INVALID = "secret_invalid"

_HEADER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_compare = hmac.compare_digest  # a test counts the calls through this name

SIGNED_PAYLOADS = ("{body}", "{timestamp}.{body}", "{timestamp}:{body}", "{id}.{timestamp}.{body}")


class TimestampConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    header: str
    unit: Literal["seconds", "milliseconds"] = "seconds"

    @model_validator(mode="after")
    def _header(self) -> "TimestampConfig":
        if not _HEADER_NAME.match(self.header.lower()):
            raise ValueError("timestamp.header is not a header name")
        return self


class IdConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    header: str

    @model_validator(mode="after")
    def _header(self) -> "IdConfig":
        if not _HEADER_NAME.match(self.header.lower()):
            raise ValueError("id.header is not a header name")
        return self


class CustomConfig(BaseModel):
    """The `custom` preset: a closed model (design 4.3). No free-form templates, no user-supplied code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    header: str = "x-signature-256"
    prefix: str = Field(default="sha256=", max_length=32)
    algorithm: Literal["sha256", "sha512"] = "sha256"
    encoding: Literal["hex", "base64"] = "hex"
    key_encoding: Literal["utf8", "base64", "hex"] = "utf8"
    timestamp: TimestampConfig | None = None
    id: IdConfig | None = None
    signed_payload: Literal["{body}", "{timestamp}.{body}", "{timestamp}:{body}", "{id}.{timestamp}.{body}"] = "{body}"
    allow_body_hash_dedupe_only: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "CustomConfig":
        if not _HEADER_NAME.match(self.header.lower()):
            raise ValueError("header is not a header name")
        if "{timestamp}" in self.signed_payload and self.timestamp is None:
            raise ValueError("signed_payload uses {timestamp} but no timestamp header is configured")
        if "{id}" in self.signed_payload and self.id is None:
            raise ValueError("signed_payload uses {id} but no id header is configured")
        if self.timestamp is None and "{id}" not in self.signed_payload and not self.allow_body_hash_dedupe_only:
            raise ValueError(
                "replay_protection_required: sign a timestamp or a signed id, or accept dedupe by body hash with "
                "allow_body_hash_dedupe_only"
            )
        return self


@dataclass(frozen=True)
class Verified:
    """The outcome of `verify`. `signature_ok` says a signature matched; `reason` is None only when the delivery is also
    inside the tolerance window. A stale delivery has `signature_ok=True` and `reason="timestamp_stale"`."""

    signature_ok: bool
    reason: str | None = None
    secret_slot: int | None = None
    timestamp: int | None = None  # seconds
    signed_id: str | None = None
    skew_seconds: int | None = None

    @property
    def ok(self) -> bool:
        return self.signature_ok and self.reason is None


def _fail(reason: str) -> Verified:
    return Verified(signature_ok=False, reason=reason)


def constant_time_equals(a: bytes, b: bytes) -> bool:
    return _compare(a, b)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name.lower())
    if value is None:
        for key, candidate in headers.items():
            if key.lower() == name.lower():
                return candidate
        return None
    return value


def _bounded(value: str) -> bool:
    return len(value.encode("utf-8", errors="replace")) <= MAX_HEADER_BYTES


def _decode_key(secret: str, encoding: str) -> bytes | None:
    try:
        if encoding == "utf8":
            return secret.encode("utf-8")
        if encoding == "hex":
            return bytes.fromhex(secret)
        return base64.b64decode(secret, validate=True)
    except (ValueError, binascii.Error):
        return None


def _digest(algorithm: str, key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, getattr(hashlib, algorithm)).digest()


def _encode(digest: bytes, encoding: str) -> bytes:
    return digest.hex().encode() if encoding == "hex" else base64.b64encode(digest)


def _integer(value: str) -> int | None:
    value = value.strip()
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 16 else None


def _match(expected_by_secret: Sequence[bytes | None], candidates: Sequence[bytes]) -> int | None:
    """The slot of the first secret any candidate matched, or None. Every (secret, candidate) pair is compared."""
    matched: int | None = None
    for slot, expected in enumerate(expected_by_secret):
        probe = expected if expected is not None else b"\x00"
        hit = False
        for candidate in candidates or (b"",):
            hit |= _compare(probe, candidate) and expected is not None
        if hit and matched is None:
            matched = slot
    return matched


def _window(timestamp: int | None, now: float, tolerance: int, base: Verified) -> Verified:
    if timestamp is None:
        return base
    skew = int(now) - timestamp
    if abs(skew) > tolerance:
        return Verified(True, REASON_STALE, base.secret_slot, timestamp, base.signed_id, skew)
    return Verified(True, None, base.secret_slot, timestamp, base.signed_id, skew)


def verify(
    preset: str,
    config: CustomConfig | Mapping[str, object] | None,
    headers: Mapping[str, str],
    raw_body: bytes,
    secrets: Sequence[str | None],
    now: float,
    tolerance: int = 300,
) -> Verified:
    """Verify one delivery. `secrets` holds the trigger's one or two secret values in slot order; None is a reference that
    did not resolve (`secret_missing`). Never raises on attacker-controlled input."""
    if not 1 <= len(secrets) <= MAX_SECRETS:
        return _fail(REASON_SECRET_MISSING)
    if all(s is None for s in secrets):
        return _fail(REASON_SECRET_MISSING)
    if preset == "github":
        return _verify_github(headers, raw_body, secrets)
    if preset == "standard_webhooks":
        return _verify_standard(headers, raw_body, secrets, now, tolerance)
    if preset == "custom":
        parsed = config if isinstance(config, CustomConfig) else CustomConfig.model_validate(config or {})
        return _verify_custom(parsed, headers, raw_body, secrets, now, tolerance)
    return _fail(REASON_MALFORMED)


# --- github ---------------------------------------------------------------------------------------------------

def _verify_github(headers: Mapping[str, str], body: bytes, secrets: Sequence[str | None]) -> Verified:
    header = _header(headers, "x-hub-signature-256")
    if header is None:
        return _fail(REASON_MISSING_HEADER)
    if not _bounded(header) or not header.startswith("sha256="):
        return _fail(REASON_MALFORMED)
    candidate = header[len("sha256="):].strip().lower()
    if len(candidate) != 64 or any(c not in "0123456789abcdef" for c in candidate):
        return _fail(REASON_MALFORMED)
    expected = [None if s is None else _digest("sha256", s.encode("utf-8"), body).hex().encode() for s in secrets]
    slot = _match(expected, [candidate.encode()])
    return Verified(True, None, slot) if slot is not None else _fail(REASON_MISMATCH)


# --- standard webhooks ----------------------------------------------------------------------------------------

def standard_webhooks_key(secret: str) -> bytes | None:
    """The HMAC key of a `whsec_<base64>` secret, or None when it does not decode (checked at trigger creation)."""
    if not secret.startswith("whsec_"):
        return None
    try:
        key = base64.b64decode(secret[len("whsec_"):], validate=True)
    except (ValueError, binascii.Error):
        return None
    return key or None


def _verify_standard(
    headers: Mapping[str, str], body: bytes, secrets: Sequence[str | None], now: float, tolerance: int
) -> Verified:
    msg_id = _header(headers, "webhook-id")
    raw_ts = _header(headers, "webhook-timestamp")
    raw_sig = _header(headers, "webhook-signature")
    if msg_id is None or raw_ts is None or raw_sig is None:
        return _fail(REASON_MISSING_HEADER)
    if not (_bounded(msg_id) and _bounded(raw_ts) and _bounded(raw_sig)):
        return _fail(REASON_MALFORMED)
    timestamp = _integer(raw_ts)
    if timestamp is None or not msg_id:
        return _fail(REASON_MALFORMED)
    entries = raw_sig.split()
    if len(entries) > MAX_CANDIDATES:
        return _fail(REASON_MALFORMED)
    candidates = [e[len("v1,"):].encode() for e in entries if e.startswith("v1,")]  # v1a (Ed25519) and others ignored
    if not candidates:
        return _fail(REASON_MALFORMED)
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected: list[bytes | None] = []
    any_key = False
    for secret in secrets:
        key = None if secret is None else standard_webhooks_key(secret)
        any_key = any_key or key is not None
        expected.append(None if key is None else base64.b64encode(_digest("sha256", key, signed)))
    if not any_key:
        return _fail(REASON_SECRET_INVALID)
    slot = _match(expected, candidates)
    if slot is None:
        return _fail(REASON_MISMATCH)
    return _window(timestamp, now, tolerance, Verified(True, None, slot, timestamp, msg_id))


# --- custom ---------------------------------------------------------------------------------------------------

def _verify_custom(
    config: CustomConfig, headers: Mapping[str, str], body: bytes, secrets: Sequence[str | None], now: float,
    tolerance: int,
) -> Verified:
    raw = _header(headers, config.header)
    if raw is None:
        return _fail(REASON_MISSING_HEADER)
    if not _bounded(raw):
        return _fail(REASON_MALFORMED)
    raw = raw.strip()
    if config.prefix:
        if not raw.startswith(config.prefix):
            return _fail(REASON_MALFORMED)
        raw = raw[len(config.prefix):]
    if not raw:
        return _fail(REASON_MALFORMED)
    timestamp: int | None = None
    ts_text = ""
    if config.timestamp is not None:
        ts_header = _header(headers, config.timestamp.header)
        if ts_header is None:
            return _fail(REASON_MISSING_HEADER)
        ts_value = _integer(ts_header) if _bounded(ts_header) else None
        if ts_value is None:
            return _fail(REASON_MALFORMED)
        ts_text = str(ts_value)
        timestamp = ts_value // 1000 if config.timestamp.unit == "milliseconds" else ts_value
    signed_id: str | None = None
    if config.id is not None:
        signed_id = _header(headers, config.id.header)
        if signed_id is None and "{id}" in config.signed_payload:
            return _fail(REASON_MISSING_HEADER)
        if signed_id is not None and (not _bounded(signed_id) or not signed_id):
            return _fail(REASON_MALFORMED)
    template = config.signed_payload.encode()
    # `{body}` is replaced last: the body (attacker bytes) is never scanned for the other placeholders.
    message = template.replace(b"{id}", (signed_id or "").encode()).replace(b"{timestamp}", ts_text.encode()).replace(b"{body}", body)
    candidates = [raw.encode()]
    expected: list[bytes | None] = []
    any_key = False
    for secret in secrets:
        key = None if secret is None else _decode_key(secret, config.key_encoding)
        any_key = any_key or key is not None
        expected.append(None if key is None else _encode(_digest(config.algorithm, key, message), config.encoding))
    if not any_key:
        return _fail(REASON_SECRET_INVALID)
    if config.encoding == "hex":
        candidates = [raw.lower().encode()]
    slot = _match(expected, candidates)
    if slot is None:
        return _fail(REASON_MISMATCH)
    signed = signed_id if "{id}" in config.signed_payload else None
    return _window(timestamp, now, tolerance, Verified(True, None, slot, timestamp, signed))


# --- dedupe ---------------------------------------------------------------------------------------------------

def body_sha256(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def _bounded_key(kind: str, value: str) -> str:
    key = f"{kind}:{value}"
    return key if len(key) <= MAX_DEDUPE_KEY_CHARS else f"{kind}:{hashlib.sha256(value.encode()).hexdigest()}"


def dedupe_key(preset: str, verified: Verified, raw_body: bytes) -> tuple[str, str]:
    """(`key`, `kind`) for a verified delivery, built only from signed material (design 4.4): GitHub dedupes on the body
    hash (its delivery GUID is unsigned), Standard Webhooks on the signed `webhook-id`, `custom` on a signed id when the
    signed template includes one and on the body hash otherwise. A timestamp is never part of it."""
    if verified.signed_id:
        return _bounded_key("id", verified.signed_id), "id"
    return f"body:{body_sha256(raw_body)}", "body"


__all__ = [
    "MAX_CANDIDATES", "MAX_DEDUPE_KEY_CHARS", "MAX_HEADER_BYTES", "MAX_SECRETS", "PRESETS", "Preset",
    "REASON_MALFORMED", "REASON_MISMATCH", "REASON_MISSING_HEADER", "REASON_SECRET_INVALID", "REASON_SECRET_MISSING",
    "REASON_STALE", "SIGNED_PAYLOADS", "CustomConfig", "IdConfig", "TimestampConfig", "Verified", "body_sha256",
    "constant_time_equals", "dedupe_key", "standard_webhooks_key", "verify",
]
