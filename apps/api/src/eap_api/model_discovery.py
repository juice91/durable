"""Model-provider discovery: what each provider offers, and whether it is reachable.

Used by GET /models (expands `provider:*` allowlist wildcards into real model names) and by
GET /providers and POST /providers/{id}/test (the Studio Providers page). Server-side only: the
provider keys come from the API's environment and never leave this module. Responses carry the
env var NAME and the base URL HOST, never a key value, and error text is a fixed vocabulary
(never an exception message, which could echo a URL or a header).

Providers (the same environment the worker reads; see eap_worker.models / config):
- `litellm`: EAP_LITELLM_BASE_URL (fallback OPENAI_BASE_URL) + EAP_LITELLM_API_KEY (fallback
  LITELLM_API_KEY), listed at `<base>/v1/models` (a base that already ends in `/v1` is not doubled).
- `anthropic`: ANTHROPIC_API_KEY, https://api.anthropic.com/v1/models.
- `openai`: OPENAI_API_KEY, https://api.openai.com/v1/models.

A successful check is cached per provider for CACHE_TTL_S. A failed or unconfigured check is
kept only for FAILURE_TTL_S, so a transient outage does not leave wildcard providers in free-text
mode for the full TTL. POST /providers/{id}/test refreshes the cache, but a result younger than
REFRESH_COOLDOWN_S is reused so repeated tests cannot hammer the provider.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

CACHE_TTL_S = 300.0
REQUEST_TIMEOUT_S = 5.0

OK = "ok"
UNREACHABLE = "unreachable"
NOT_CONFIGURED = "not_configured"

# (url, headers, timeout_s) -> parsed JSON body. Raises HttpStatusError for a non-2xx answer; any
# other exception counts as a connection failure. Tests replace `http_get` with a stub.
HttpGet = Callable[[str, Mapping[str, str], float], Any]


class HttpStatusError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(status)


def default_http_get(url: str, headers: Mapping[str, str], timeout_s: float) -> Any:
    import httpx2

    response = httpx2.get(url, headers=dict(headers), timeout=timeout_s, follow_redirects=False)
    if response.status_code >= 300:
        raise HttpStatusError(response.status_code)
    return response.json()


http_get: HttpGet = default_http_get


@dataclass(frozen=True)
class ProviderDef:
    id: str
    label: str
    key_envs: tuple[str, ...]
    base_url_envs: tuple[str, ...]
    default_base_url: str | None
    header_style: str  # "bearer" | "anthropic"


PROVIDERS: tuple[ProviderDef, ...] = (
    ProviderDef("litellm", "LiteLLM gateway", ("EAP_LITELLM_API_KEY", "LITELLM_API_KEY"),
                ("EAP_LITELLM_BASE_URL", "OPENAI_BASE_URL"), None, "bearer"),
    ProviderDef("anthropic", "Anthropic", ("ANTHROPIC_API_KEY",), (), "https://api.anthropic.com", "anthropic"),
    ProviderDef("openai", "OpenAI", ("OPENAI_API_KEY",), (), "https://api.openai.com", "bearer"),
)
PROVIDER_BY_ID: dict[str, ProviderDef] = {p.id: p for p in PROVIDERS}


@dataclass(frozen=True)
class ProviderStatus:
    id: str
    label: str
    configured: bool
    env_var: str
    base_url_host: str | None
    status: str
    models: tuple[str, ...]
    checked_at: str | None
    error: str | None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "configured": self.configured, "env_var": self.env_var,
            "base_url_host": self.base_url_host, "status": self.status, "model_count": len(self.models),
            "checked_at": self.checked_at, "error": self.error,
        }


def _first_set(names: tuple[str, ...], environ: Mapping[str, str]) -> str | None:
    for name in names:
        if (environ.get(name) or "").strip():
            return name
    return None


def _host(url: str | None) -> str | None:
    """`host[:port]` of a URL: no scheme, path, query or credentials."""
    if not url:
        return None
    try:
        parts = urlsplit(url if "//" in url else f"//{url}")
        host = parts.hostname
        if not host:
            return None
        return f"{host}:{parts.port}" if parts.port else host
    except ValueError:
        return None


def _list_url(base: str) -> str:
    base = base.rstrip("/")
    return f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"


def _model_names(body: Any) -> list[str]:
    """Names out of an OpenAI-style (`data[].id`) or Anthropic-style list body."""
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise ValueError("unexpected response")
    names = {row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]}
    return sorted(names)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _check(provider: ProviderDef, environ: Mapping[str, str]) -> ProviderStatus:
    key_env = _first_set(provider.key_envs, environ)
    base_env = _first_set(provider.base_url_envs, environ)
    base = environ[base_env].strip() if base_env else provider.default_base_url
    env_var = key_env or provider.key_envs[0]
    host = _host(base)
    common = {"id": provider.id, "label": provider.label, "env_var": env_var, "base_url_host": host}
    if not key_env or not base:
        return ProviderStatus(**common, configured=False, status=NOT_CONFIGURED, models=(), checked_at=None,
                              error="no API key is set on the server" if not key_env else "no base URL is set on the server")
    key = environ[key_env].strip()
    headers = (
        {"x-api-key": key, "anthropic-version": "2023-06-01"} if provider.header_style == "anthropic"
        else {"Authorization": f"Bearer {key}"}
    )
    url = _list_url(base) + ("?limit=1000" if provider.id == "anthropic" else "")
    try:
        names = _model_names(http_get(url, headers, REQUEST_TIMEOUT_S))
    except HttpStatusError as exc:
        error = "the provider rejected the server's key" if exc.status in (401, 403) else f"the provider answered HTTP {exc.status}"
        return ProviderStatus(**common, configured=True, status=UNREACHABLE, models=(), checked_at=_now(), error=error)
    except ValueError:
        return ProviderStatus(**common, configured=True, status=UNREACHABLE, models=(), checked_at=_now(),
                              error="the provider answered with an unexpected model list")
    except Exception as exc:  # noqa: BLE001 - never echo the exception text (may carry URLs)
        name = type(exc).__name__.lower()
        error = "the provider did not answer in time" if "timeout" in name else "could not connect to the provider"
        return ProviderStatus(**common, configured=True, status=UNREACHABLE, models=(), checked_at=_now(), error=error)
    return ProviderStatus(**common, configured=True, status=OK, models=tuple(names), checked_at=_now(), error=None)


FAILURE_TTL_S = 30.0
REFRESH_COOLDOWN_S = 10.0

_cache: dict[str, tuple[float, ProviderStatus]] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def provider_status(provider_id: str, *, refresh: bool = False, environ: Mapping[str, str] | None = None) -> ProviderStatus:
    provider = PROVIDER_BY_ID[provider_id]
    env = os.environ if environ is None else environ
    now = time.monotonic()
    with _lock:
        hit = _cache.get(provider_id)
    if hit:
        age = now - hit[0]
        # A failed or unconfigured result is kept only briefly, so a transient outage does not hold
        # wildcard providers in free-text mode for the full TTL.
        ttl = CACHE_TTL_S if hit[1].status == OK else FAILURE_TTL_S
        # An explicit refresh (POST /providers/{id}/test) still reuses a result younger than the
        # cooldown, so repeated clicks or scripted calls cannot hammer the provider. A normal read
        # follows ttl alone, which is what lets a failed check expire on its shorter timer.
        if refresh:
            if age < REFRESH_COOLDOWN_S:
                return hit[1]
        elif age < ttl:
            return hit[1]
    result = _check(provider, env)
    with _lock:
        _cache[provider_id] = (now, result)
    return result


def all_provider_statuses(*, refresh: bool = False) -> list[ProviderStatus]:
    return [provider_status(p.id, refresh=refresh) for p in PROVIDERS]
