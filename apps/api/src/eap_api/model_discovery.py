"""Model-provider discovery: what each provider offers, and whether it is reachable.

Used by GET /models (expands `provider:*` allowlist wildcards into real model names) and by
GET /providers and POST /providers/{id}/test (the Studio Providers page). Server-side only: the
provider keys come from the API's environment and never leave this module. Responses carry the
env var NAME and the base URL HOST, never a key value, and error text is a fixed vocabulary
(never an exception message, which could echo a URL or a header).

Providers (the same environment the worker reads; see eap_worker.models / config):
- `litellm`: EAP_LITELLM_BASE_URL (fallback OPENAI_BASE_URL) + EAP_LITELLM_API_KEY (fallback
  LITELLM_API_KEY), listed at `<base>/v1/models` (a base that already ends in `/v1` is not doubled).
- `anthropic`: ANTHROPIC_API_KEY, https://api.anthropic.com/v1/models (the models the key can use, all
  pages via `after_id`); when that listing fails a small fallback set (ANTHROPIC_FALLBACK) is offered,
  marked source "fallback", and the status keeps its error.
- `openai`: OPENAI_API_KEY, https://api.openai.com/v1/models.
- `cloudflare`: Workers AI (optionally through AI Gateway). Token CLOUDFLARE_API_TOKEN (fallback
  EAP_CLOUDFLARE_API_TOKEN) and EAP_CLOUDFLARE_ACCOUNT_ID are both required; EAP_CLOUDFLARE_GATEWAY_ID adds the
  `cf-aig-gateway-id` header on every request and EAP_CLOUDFLARE_BASE_URL overrides the OpenAI-compatible base
  (default `https://api.cloudflare.com/client/v4/accounts/<account>/ai/v1`). Models come from the account's
  catalogue (`/ai/models/search?task=Text Generation`, up to CLOUDFLARE_MAX_PAGES pages), ids as returned
  (`@cf/...`), with function-calling and context size read from each model's `properties`.

Each discovered model carries `Capabilities` (image and PDF input, and where that came from):
`gateway` (LiteLLM `GET <root>/model/info`: `supports_vision`, `supports_pdf_input`, fetched with the
model list and cached with it), `static` (a table of the Anthropic and OpenAI families) or `assumed`
(the old provider rules of eap_run_store.chat_attachments.model_accepts, used for anything else and
whenever `/model/info` fails). A failed `/model/info` never fails discovery.

A successful check is cached per provider for CACHE_TTL_S. A failed or unconfigured check is
kept only for FAILURE_TTL_S, so a transient outage does not leave wildcard providers in free-text
mode for the full TTL. POST /providers/{id}/test refreshes the cache, but a result younger than
REFRESH_COOLDOWN_S is reused so repeated tests cannot hammer the provider.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

from eap_run_store.chat_attachments import IMAGE_TYPES, PDF_TYPE, model_accepts
from eap_schemas.models_policy import ANTHROPIC_FALLBACK

log = logging.getLogger(__name__)

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
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
CLOUDFLARE_TOKEN_ENVS = ("CLOUDFLARE_API_TOKEN", "EAP_CLOUDFLARE_API_TOKEN")
CLOUDFLARE_ACCOUNT_ENV = "EAP_CLOUDFLARE_ACCOUNT_ID"
CLOUDFLARE_GATEWAY_ENV = "EAP_CLOUDFLARE_GATEWAY_ID"
CLOUDFLARE_BASE_URL_ENV = "EAP_CLOUDFLARE_BASE_URL"
CLOUDFLARE_PAGE_SIZE = 100
CLOUDFLARE_MAX_PAGES = 5
PROVIDERS = PROVIDERS + (
    ProviderDef("cloudflare", "Cloudflare Workers AI", CLOUDFLARE_TOKEN_ENVS, (CLOUDFLARE_BASE_URL_ENV,), None, "bearer"),
)
PROVIDER_BY_ID: dict[str, ProviderDef] = {p.id: p for p in PROVIDERS}


@dataclass(frozen=True)
class Capabilities:
    """Whether a model takes images and PDFs as input, and the basis: "gateway" | "static" | "assumed"."""

    image: bool
    pdf: bool
    source: str

    def public(self) -> dict[str, Any]:
        return {"image": self.image, "pdf": self.pdf, "source": self.source}


@dataclass(frozen=True)
class ModelEntry:
    name: str
    capabilities: Capabilities
    # "discovered" (the provider listed it) or "fallback" (the small static set used when its listing failed)
    source: str = "discovered"
    # Reported by providers that publish them (Cloudflare's catalogue); None = unknown.
    function_calling: bool | None = None
    structured_output: bool | None = None
    context_window: int | None = None


# Families known to take images (and PDFs), per provider; (pattern, image, pdf), first match wins.
# Anthropic `^claude-(3|4|opus|sonnet|haiku)`: the Claude 3 and 4 generations and the later
#   unnumbered-family names (claude-fable-5-2, claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5) read images and
#   PDFs; claude-2 and claude-instant take no files, so they are not matched (they fall to `assumed`).
# OpenAI `gpt-4o`, `gpt-4.1`, `gpt-4.5`, `gpt-5`, `chatgpt-4o`: vision models; `o1`, `o3`, `o4` reasoning
#   models with image input (`o1-mini` and `o3-mini` are text-only and excluded by the lookahead).
#   PDF is False: PDF input over OpenAI's chat API depends on the endpoint, so it is only offered when
#   the gateway reports `supports_pdf_input` (the plan's wording); the safe side is to refuse the file.
STATIC_CAPABILITIES: dict[str, tuple[tuple[re.Pattern[str], bool, bool], ...]] = {
    "anthropic": ((re.compile(r"^claude-(3|4|opus|sonnet|haiku|fable)"), True, True),),
    "openai": (
        (re.compile(r"^(gpt-4o|gpt-4\.1|gpt-4\.5|gpt-5|chatgpt-4o|o1(?!-mini)|o3(?!-mini)|o4)"), True, False),
    ),
}


def static_capabilities(provider: str, name: str) -> Capabilities | None:
    for pattern, image, pdf in STATIC_CAPABILITIES.get(provider, ()):
        if pattern.match(name):
            return Capabilities(image, pdf, "static")
    return None


def assumed_capabilities(provider: str, name: str) -> Capabilities:
    """The old rules (model_accepts): images for any real provider, PDFs where the provider reads them."""
    return Capabilities(
        image=model_accepts(provider, name, next(iter(sorted(IMAGE_TYPES)))),
        pdf=model_accepts(provider, name, PDF_TYPE),
        source="assumed",
    )


@dataclass(frozen=True)
class ProviderStatus:
    id: str
    label: str
    configured: bool
    env_var: str
    base_url_host: str | None
    status: str
    models: tuple[ModelEntry, ...]
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


def _info_url(base: str) -> str:
    """The gateway root's `/model/info` (the base's trailing `/v1` is dropped)."""
    base = base.rstrip("/")
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    return f"{root}/model/info"


def _gateway_info(body: Any) -> dict[str, dict[str, Any]]:
    """`model_name` -> `model_info` out of a LiteLLM /model/info body. A model served by several
    deployments has several rows: their `supports_vision` / `supports_pdf_input` flags are ANDed (the
    most conservative answer, since a request may land on any of them; a flag a row leaves out is
    ignored). Rows are matched by exact `model_name`, so a wildcard row (`claude-*`) never matches a
    model and those models fall to the static table or `assumed`."""
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise ValueError("unexpected response")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        info = row.get("model_info") if isinstance(row, dict) else None
        if isinstance(row, dict) and isinstance(row.get("model_name"), str) and isinstance(info, dict):
            name = row["model_name"]
            if name not in out:
                out[name] = dict(info)
                continue
            merged = out[name]
            for flag in ("supports_vision", "supports_pdf_input"):
                a, b = merged.get(flag), info.get(flag)
                if a is None or b is None:
                    merged[flag] = b if a is None else a
                else:
                    merged[flag] = bool(a) and bool(b)
    return out


def _entry(provider: str, name: str, gateway: dict[str, dict[str, Any]]) -> ModelEntry:
    assumed = assumed_capabilities(provider, name)
    info = gateway.get(name)
    if info is not None and (info.get("supports_vision") is not None or info.get("supports_pdf_input") is not None):
        vision, pdf = info.get("supports_vision"), info.get("supports_pdf_input")
        return ModelEntry(name, Capabilities(
            image=assumed.image if vision is None else bool(vision),
            pdf=assumed.pdf if pdf is None else bool(pdf),
            source="gateway",
        ))
    return ModelEntry(name, static_capabilities(provider, name) or assumed)


def _fallback(provider: str) -> tuple[ModelEntry, ...]:
    """Anthropic's small static set, offered only when its own listing cannot be read."""
    if provider != "anthropic":
        return ()
    return tuple(
        ModelEntry(n, static_capabilities(provider, n) or assumed_capabilities(provider, n), "fallback")
        for n in ANTHROPIC_FALLBACK
    )


ANTHROPIC_PAGE_LIMIT = 1000
ANTHROPIC_MAX_PAGES = 20


def _anthropic_names(url: str, headers: Mapping[str, str]) -> list[str]:
    """Every model id the key can use: `GET /v1/models?limit=N`, following `after_id` while `has_more`."""
    names: set[str] = set()
    after: str | None = None
    for _ in range(ANTHROPIC_MAX_PAGES):
        page_url = f"{url}?limit={ANTHROPIC_PAGE_LIMIT}" + (f"&after_id={quote(after)}" if after else "")
        body = http_get(page_url, headers, REQUEST_TIMEOUT_S)
        names.update(_model_names(body))
        last = body.get("last_id") if isinstance(body, dict) else None
        if not (isinstance(body, dict) and body.get("has_more") is True and isinstance(last, str) and last and last != after):
            break
        after = last
    return sorted(names)


# Workers AI text models whose catalogue `properties` may be missing; facts from Cloudflare's model page.
CLOUDFLARE_STATIC: dict[str, dict[str, Any]] = {
    "@cf/deepseek-ai/deepseek-v4.1-flash-1017": {
        "function_calling": True, "structured_output": True, "context_window": 1_048_576,
    },
}
# Text-only unless the catalogue says otherwise: the generic "assumed" rule would offer images to every model.
_CLOUDFLARE_TEXT_ONLY = Capabilities(image=False, pdf=False, source="static")


def _property_map(properties: Any) -> dict[str, Any]:
    """Cloudflare's `properties` is a list of `{property_id, value}` (values are strings); a plain dict is accepted too."""
    if isinstance(properties, dict):
        return dict(properties)
    out: dict[str, Any] = {}
    if isinstance(properties, list):
        for row in properties:
            if isinstance(row, dict) and isinstance(row.get("property_id"), str):
                out[row["property_id"]] = row.get("value")
    return out


def _truthy(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _cloudflare_entry(row: dict[str, Any]) -> ModelEntry:
    name = row["name"]
    props = _property_map(row.get("properties"))
    static = CLOUDFLARE_STATIC.get(name, {})
    function_calling = _truthy(props.get("function_calling"))
    context = props.get("context_window", props.get("max_total_tokens"))
    try:
        context_window = int(str(context).replace(",", "")) if context is not None else None
    except ValueError:
        context_window = None
    vision = _truthy(props.get("vision"))
    caps = Capabilities(True, False, "gateway") if vision else _CLOUDFLARE_TEXT_ONLY
    return ModelEntry(
        name, caps,
        function_calling=function_calling if function_calling is not None else static.get("function_calling"),
        structured_output=static.get("structured_output"),
        context_window=context_window or static.get("context_window"),
    )


def _cloudflare_models(account: str, headers: Mapping[str, str]) -> tuple[ModelEntry, ...]:
    """Text-generation models of the account: `GET /accounts/<id>/ai/models/search`, up to CLOUDFLARE_MAX_PAGES pages."""
    rows: dict[str, dict[str, Any]] = {}
    for page in range(1, CLOUDFLARE_MAX_PAGES + 1):
        url = (f"{CLOUDFLARE_API}/accounts/{quote(account, safe='')}/ai/models/search"
               f"?task=Text%20Generation&per_page={CLOUDFLARE_PAGE_SIZE}&page={page}")
        body = http_get(url, headers, REQUEST_TIMEOUT_S)
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, list):
            raise ValueError("unexpected response")
        for row in result:
            if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"]:
                rows.setdefault(row["name"], row)
        info = body.get("result_info") if isinstance(body.get("result_info"), dict) else {}
        total = info.get("total_count")
        if len(result) < CLOUDFLARE_PAGE_SIZE or (isinstance(total, int) and page * CLOUDFLARE_PAGE_SIZE >= total):
            break
    return tuple(_cloudflare_entry(rows[n]) for n in sorted(rows))


def _check_cloudflare(provider: ProviderDef, environ: Mapping[str, str]) -> ProviderStatus:
    key_env = _first_set(provider.key_envs, environ)
    account = (environ.get(CLOUDFLARE_ACCOUNT_ENV) or "").strip()
    base = (environ.get(CLOUDFLARE_BASE_URL_ENV) or "").strip() or (
        f"{CLOUDFLARE_API}/accounts/{quote(account, safe='')}/ai/v1" if account else None)
    common = {"id": provider.id, "label": provider.label, "env_var": key_env or provider.key_envs[0],
              "base_url_host": _host(base or CLOUDFLARE_API)}
    if not key_env or not account:
        missing = provider.key_envs[0] if not key_env else CLOUDFLARE_ACCOUNT_ENV
        return ProviderStatus(**common, configured=False, status=NOT_CONFIGURED, models=(), checked_at=None,
                              error=f"{missing} is not set on the server")
    headers = {"Authorization": f"Bearer {environ[key_env].strip()}"}
    gateway = (environ.get(CLOUDFLARE_GATEWAY_ENV) or "").strip()
    if gateway:
        headers["cf-aig-gateway-id"] = gateway
    try:
        models = _cloudflare_models(account, headers)
    except HttpStatusError as exc:
        error = "the provider rejected the server's key" if exc.status in (401, 403) else f"the provider answered HTTP {exc.status}"
        return ProviderStatus(**common, configured=True, status=UNREACHABLE, models=(), checked_at=_now(), error=error)
    except ValueError:
        return ProviderStatus(**common, configured=True, status=UNREACHABLE, models=(), checked_at=_now(),
                              error="the provider answered with an unexpected model list")
    except Exception as exc:  # noqa: BLE001 - never echo the exception text (may carry URLs)
        error = "the provider did not answer in time" if "timeout" in type(exc).__name__.lower() else "could not connect to the provider"
        return ProviderStatus(**common, configured=True, status=UNREACHABLE, models=(), checked_at=_now(), error=error)
    return ProviderStatus(**common, configured=True, status=OK, models=models, checked_at=_now(), error=None)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fetch_gateway_info(provider: ProviderDef, base: str, headers: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """The LiteLLM gateway's per-model capability info, once per refresh and with the model list's
    timeout. Any failure is logged (no detail: it may carry a URL) and leaves the models `assumed`."""
    if provider.id != "litellm":
        return {}
    try:
        return _gateway_info(http_get(_info_url(base), headers, REQUEST_TIMEOUT_S))
    except Exception as exc:  # noqa: BLE001 - never fail discovery for the capability lookup
        log.warning("model capability lookup failed (%s); models are assumed", type(exc).__name__)
        return {}


def _check(provider: ProviderDef, environ: Mapping[str, str]) -> ProviderStatus:
    if provider.id == "cloudflare":
        return _check_cloudflare(provider, environ)
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
    url = _list_url(base)
    # A failed listing leaves the status unreachable (the Providers page tells the truth) but Anthropic's
    # small fallback set is still offered.
    fallback = _fallback(provider.id)
    try:
        names = _anthropic_names(url, headers) if provider.id == "anthropic" else _model_names(http_get(url, headers, REQUEST_TIMEOUT_S))
        gateway = _fetch_gateway_info(provider, base, headers)
    except HttpStatusError as exc:
        error = "the provider rejected the server's key" if exc.status in (401, 403) else f"the provider answered HTTP {exc.status}"
        return ProviderStatus(**common, configured=True, status=UNREACHABLE, models=fallback, checked_at=_now(), error=error)
    except ValueError:
        return ProviderStatus(**common, configured=True, status=UNREACHABLE, models=fallback, checked_at=_now(),
                              error="the provider answered with an unexpected model list")
    except Exception as exc:  # noqa: BLE001 - never echo the exception text (may carry URLs)
        name = type(exc).__name__.lower()
        error = "the provider did not answer in time" if "timeout" in name else "could not connect to the provider"
        return ProviderStatus(**common, configured=True, status=UNREACHABLE, models=fallback, checked_at=_now(), error=error)
    models = tuple(_entry(provider.id, n, gateway) for n in names)
    return ProviderStatus(**common, configured=True, status=OK, models=models, checked_at=_now(), error=None)


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


def model_names(status: ProviderStatus) -> list[str]:
    return [m.name for m in status.models]


def capabilities_for(provider: str, name: str) -> Capabilities:
    """What a model takes as input. A discoverable provider's cached listing answers first (one
    discovery refresh serves the model list and these, same TTLs); a model it does not list, a failed
    or missing discovery and every other provider (`test:`, ...) fall back to the static table, then
    the assumed old provider rules."""
    if provider in PROVIDER_BY_ID:
        status = provider_status(provider)
        if status.status == OK:
            for entry in status.models:
                if entry.name == name:
                    return entry.capabilities
    return static_capabilities(provider, name) or assumed_capabilities(provider, name)
