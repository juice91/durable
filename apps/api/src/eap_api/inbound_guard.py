"""The guard of the public routes, inbound webhooks (design 4.1, 4.2 steps 1 and 3, 8.2) and the external agent surface
(external design 8): protocol-level, no business rules.

- per-IP, per-trigger and per-key token buckets (in memory, per API process: with several replicas the effective rate is the limit
  times the replica count, a guard against a runaway sender, not billing);
- counters, not rows, for unauthenticated noise (bad signatures, missing headers, unknown ids): an attacker on the
  public internet must not write to the database at the rate of their bandwidth. Counters are in memory (since the
  process started), bounded in keys;
- the uniform answers: 404 `{"detail": "not found"}`, 401 `{"detail": "invalid signature"}`;
- the startup rules: `EAP_WEBHOOK_BASE_URL` is https (http only for a loopback host), and a deployment that exposes the
  hook path publicly (`EAP_WEBHOOK_PUBLIC=true`) is refused under `EAP_AUTH_MODE=dev`, whose bearer is forgeable.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import threading
import time
from collections import Counter
from collections.abc import Mapping
from urllib.parse import urlsplit

MAX_BUCKETS = 10_000
MAX_COUNTER_KEYS = 5_000
DEFAULT_IP_RATE_PER_MINUTE = 120
NOT_FOUND_BODY = {"detail": "not found"}
INVALID_SIGNATURE_BODY = {"detail": "invalid signature"}
HOOKS_PREFIX = "/api/v1/hooks/"
_TRUE = frozenset({"1", "true", "yes", "on"})


class WebhookGuardConfigError(RuntimeError):
    """The webhook ingress configuration refuses to start."""


def ip_rate_from_env(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    raw = env.get("EAP_WEBHOOK_IP_RATE_PER_MINUTE", "").strip()
    try:
        return max(int(raw), 0) if raw else DEFAULT_IP_RATE_PER_MINUTE
    except ValueError:
        return DEFAULT_IP_RATE_PER_MINUTE


def trusted_proxies_from_env(env: Mapping[str, str] | None = None) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """`EAP_TRUSTED_PROXY_CIDRS` (comma list, e.g. the pod CIDR your proxy runs in): the peers whose `CF-Connecting-IP`
    is believed. Empty (the default) trusts nobody. A malformed entry fails startup."""
    env = os.environ if env is None else env
    nets = []
    for part in env.get("EAP_TRUSTED_PROXY_CIDRS", "").split(","):
        if part.strip():
            try:
                nets.append(ipaddress.ip_network(part.strip(), strict=False))
            except ValueError:
                raise WebhookGuardConfigError("EAP_TRUSTED_PROXY_CIDRS holds an entry that is not a CIDR") from None
    return tuple(nets)


def client_ip(peer: str | None, headers: Mapping[str, str], trusted: tuple = ()) -> str:
    """The key of the per-IP bucket. The TCP peer, unless it is a trusted proxy and `CF-Connecting-IP` is one valid
    address; `CF-Connecting-IP` and `X-Forwarded-For` from any other peer are ignored (a sender could set them)."""
    try:
        peer_ip = ipaddress.ip_address(peer or "")
    except ValueError:
        return peer or "unknown"
    if any(peer_ip in net for net in trusted):
        raw = (headers.get("cf-connecting-ip") or "").strip()
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            pass
    return str(peer_ip)


def webhook_base_url(env: Mapping[str, str] | None = None) -> str | None:
    """`EAP_WEBHOOK_BASE_URL` without a trailing slash, or None when unset. WebhookGuardConfigError unless it is an
    https origin (http is allowed for a loopback host, for local development)."""
    env = os.environ if env is None else env
    raw = env.get("EAP_WEBHOOK_BASE_URL", "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    loopback = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme not in ("https", "http") or not parts.hostname or (parts.scheme == "http" and not loopback):
        raise WebhookGuardConfigError("EAP_WEBHOOK_BASE_URL must be an https URL (http only for localhost)")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise WebhookGuardConfigError("EAP_WEBHOOK_BASE_URL must be a plain origin: no credentials, query or fragment")
    return raw.rstrip("/")


def check_webhook_startup(env: Mapping[str, str] | None = None, auth_mode: str | None = None) -> None:
    """Refuse a public hook path in front of the forgeable dev bearer, and a malformed base URL."""
    env = os.environ if env is None else env
    webhook_base_url(env)
    trusted_proxies_from_env(env)
    if env.get("EAP_WEBHOOK_PUBLIC", "").strip().lower() in _TRUE and (auth_mode or env.get("EAP_AUTH_MODE")) == "dev":
        raise WebhookGuardConfigError(
            "EAP_WEBHOOK_PUBLIC=true exposes /api/v1/hooks/ to the internet; refusing to start with EAP_AUTH_MODE=dev "
            "(the dev bearer is forgeable): use EAP_AUTH_MODE=production"
        )


_DUMMY_KEY = b"eap-webhook-dummy-key"
_DUMMY_BUFFER = bytes(range(256)) * 16
_DUMMY_DIGEST = hmac.new(_DUMMY_KEY, _DUMMY_BUFFER, hashlib.sha256).digest()


def dummy_work() -> None:
    """The HMAC work of an authentic failure, over a fixed buffer: an unknown or not-enabled trigger costs about what a
    bad signature costs (design 4.2 step 2), so timing does not say which ids exist."""
    hmac.compare_digest(hmac.new(_DUMMY_KEY, _DUMMY_BUFFER, hashlib.sha256).digest(), _DUMMY_DIGEST)


class InboundGuard:
    def __init__(self, ip_per_minute: int, clock=time.monotonic) -> None:  # noqa: ANN001
        self._ip_rate = ip_per_minute
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}
        self._counters: Counter[tuple[str, str]] = Counter()

    def _acquire(self, key: str, per_minute: int) -> float | None:
        if per_minute <= 0:
            return None
        now = self._clock()
        rate = per_minute / 60.0
        with self._lock:
            tokens, last = self._buckets.get(key, (float(per_minute), now))
            tokens = min(float(per_minute), tokens + (now - last) * rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                if len(self._buckets) > MAX_BUCKETS:
                    self._evict(now)
                return None
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / rate

    def _evict(self, now: float) -> None:
        for key in [k for k, (_, last) in self._buckets.items() if now - last > 120]:
            del self._buckets[key]

    def acquire_ip(self, ip: str) -> float | None:
        """None when the request may go ahead, else the seconds until a token is available."""
        return self._acquire(f"ip:{ip}", self._ip_rate)

    def acquire_trigger(self, trigger_id: str, per_minute: int) -> float | None:
        return self._acquire(f"trigger:{trigger_id}", per_minute)

    def acquire_key(self, key_id: str, per_minute: int) -> float | None:
        """The bucket of one API key (the external surface's requests per key)."""
        return self._acquire(f"key:{key_id}", per_minute)

    def acquire_card_ip(self, ip: str, per_minute: int) -> float | None:
        """The bucket of unauthenticated Agent Card requests, apart from the other requests of the address."""
        return self._acquire(f"card:{ip}", per_minute)

    def count(self, trigger_id: str | None, reason: str) -> None:
        """One refusal of an unauthenticated (or unapproved) request. `trigger_id` is None for ids that are not a known
        trigger (an unknown id must not create a counter key per probe)."""
        key = (trigger_id or "-", reason)
        with self._lock:
            if key in self._counters or len(self._counters) < MAX_COUNTER_KEYS:
                self._counters[key] += 1

    def snapshot(self, trigger_id: str | None = None) -> dict[str, int]:
        """Refusal counts by reason since the process started (for one trigger, or all)."""
        out: Counter[str] = Counter()
        with self._lock:
            for (tid, reason), n in self._counters.items():
                if trigger_id is None or tid == trigger_id:
                    out[reason] += n
        return dict(out)
