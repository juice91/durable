"""The settings of external access (docs/external-access.md), read from the environment, and the startup rules.

- `EAP_EXTERNAL_ACCESS_ENABLED` (off): whether external clients are served. A deployment that switches it on under
  `EAP_AUTH_MODE=dev`, whose bearer is forgeable, refuses to start.
- `EAP_EXTERNAL_BASE_URL`: the public https origin of the external surface. Unset: exports can be drafted, not enabled.
- Limits (platform defaults a client record may lower, or raise up to the ceiling).
- The request limits of the public surface (design 8), per API process: requests per key and per address a minute,
  A2A Agent Card requests per address a minute, and calls in flight per key.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

ENABLED_ENV = "EAP_EXTERNAL_ACCESS_ENABLED"
BASE_URL_ENV = "EAP_EXTERNAL_BASE_URL"
CLIENT_ACTIVE_ENV = "EAP_EXTERNAL_MAX_ACTIVE_RUNS_PER_CLIENT"
TENANT_ACTIVE_ENV = "EAP_EXTERNAL_MAX_ACTIVE_RUNS_PER_TENANT"
PER_DAY_ENV = "EAP_EXTERNAL_MAX_RUNS_PER_DAY"
KEY_RATE_ENV = "EAP_EXTERNAL_REQUESTS_PER_KEY_PER_MINUTE"
IP_RATE_ENV = "EAP_EXTERNAL_IP_RATE_PER_MINUTE"
CARD_RATE_ENV = "EAP_EXTERNAL_CARD_REQUESTS_PER_IP_PER_MINUTE"
KEY_CONCURRENT_ENV = "EAP_EXTERNAL_MAX_CONCURRENT_PER_KEY"
CONCURRENT_ENV = "EAP_EXTERNAL_MAX_CONCURRENT_GLOBAL"
# The worker's longest workflow run (eap_worker.workflow_run.max_run_seconds): an export's runtime stays within it.
MAX_RUN_SECONDS_ENV = "EAP_WORKFLOW_MAX_RUN_SECONDS"
DEFAULT_CLIENT_ACTIVE = 5
DEFAULT_TENANT_ACTIVE = 20
DEFAULT_CLIENT_PER_DAY = 500
DEFAULT_MAX_RUN_SECONDS = 14 * 24 * 3600
DEFAULT_KEY_RATE = 120  # requests per key a minute (0: no limit)
DEFAULT_IP_RATE = 300  # requests per client address a minute, before any credential is read (0: no limit)
DEFAULT_CARD_RATE = 60  # unauthenticated A2A Agent Card requests per client address a minute (0: no limit)
DEFAULT_KEY_CONCURRENT = 10
DEFAULT_CONCURRENT = 200
# The most a client record may raise its own limits to.
CLIENT_ACTIVE_CEILING = 50
CLIENT_PER_DAY_CEILING = 10_000
_TRUE = frozenset({"1", "true", "yes", "on"})


class ExternalConfigError(RuntimeError):
    """The external access configuration refuses to start."""


@dataclass(frozen=True)
class ExternalSettings:
    enabled: bool = False
    base_url: str | None = None
    grants_requested: bool = False  # EAP_AGENT_GRANTS_ENFORCE; the engine must keep grants too
    client_active: int = DEFAULT_CLIENT_ACTIVE
    tenant_active: int = DEFAULT_TENANT_ACTIVE
    client_per_day: int = DEFAULT_CLIENT_PER_DAY
    max_runtime_seconds: int = DEFAULT_MAX_RUN_SECONDS
    key_rate: int = DEFAULT_KEY_RATE
    ip_rate: int = DEFAULT_IP_RATE
    card_rate: int = DEFAULT_CARD_RATE
    key_concurrent: int = DEFAULT_KEY_CONCURRENT
    concurrent: int = DEFAULT_CONCURRENT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ExternalSettings":
        from eap_iam import enforcement_requested

        env = os.environ if env is None else env

        def positive(name: str, default: int) -> int:
            try:
                value = int((env.get(name) or "").strip() or default)
            except ValueError:
                return default
            return value if value >= 0 else default

        return cls(
            enabled=(env.get(ENABLED_ENV) or "").strip().lower() in _TRUE,
            base_url=external_base_url(env),
            grants_requested=enforcement_requested(env),
            client_active=positive(CLIENT_ACTIVE_ENV, DEFAULT_CLIENT_ACTIVE),
            tenant_active=positive(TENANT_ACTIVE_ENV, DEFAULT_TENANT_ACTIVE),
            client_per_day=positive(PER_DAY_ENV, DEFAULT_CLIENT_PER_DAY),
            max_runtime_seconds=positive(MAX_RUN_SECONDS_ENV, DEFAULT_MAX_RUN_SECONDS),
            key_rate=positive(KEY_RATE_ENV, DEFAULT_KEY_RATE),
            ip_rate=positive(IP_RATE_ENV, DEFAULT_IP_RATE),
            card_rate=positive(CARD_RATE_ENV, DEFAULT_CARD_RATE),
            key_concurrent=positive(KEY_CONCURRENT_ENV, DEFAULT_KEY_CONCURRENT),
            concurrent=positive(CONCURRENT_ENV, DEFAULT_CONCURRENT),
        )


def external_base_url(env: Mapping[str, str] | None = None) -> str | None:
    """`EAP_EXTERNAL_BASE_URL` without a trailing slash, or None when unset. ExternalConfigError unless it is a plain
    https URL (http is allowed for a loopback host, for local development)."""
    env = os.environ if env is None else env
    raw = (env.get(BASE_URL_ENV) or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    loopback = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme not in ("https", "http") or not parts.hostname or (parts.scheme == "http" and not loopback):
        raise ExternalConfigError(f"{BASE_URL_ENV} must be an https URL (http only for localhost)")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ExternalConfigError(f"{BASE_URL_ENV} must be a plain URL: no credentials, query or fragment")
    return raw.rstrip("/")


def check_external_startup(env: Mapping[str, str] | None = None, auth_mode: str | None = None) -> None:
    """Refuse a malformed base URL, and external access in front of the forgeable dev bearer."""
    env = os.environ if env is None else env
    external_base_url(env)
    if (env.get(ENABLED_ENV) or "").strip().lower() in _TRUE and (auth_mode or env.get("EAP_AUTH_MODE")) == "dev":
        raise ExternalConfigError(
            f"{ENABLED_ENV}=true serves external agents; refusing to start with EAP_AUTH_MODE=dev "
            "(the dev bearer is forgeable): use EAP_AUTH_MODE=production"
        )
