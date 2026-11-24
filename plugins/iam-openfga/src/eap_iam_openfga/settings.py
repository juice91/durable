"""Configuration of the OpenFGA plug-in, read from `EAP_OPENFGA_*` (design 5.6).

Selecting `EAP_IAM_POLICY=openfga` without the URL and the store id refuses to start (an engine that cannot be reached
must never be a silent allow). The model id is optional: without it OpenFGA answers with the store's latest model, which
is how a rollout between a bootstrap and the next deploy stays consistent; pin it in production."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from eap_iam import IamConfigError

ENV_URL = "EAP_OPENFGA_URL"
ENV_STORE_ID = "EAP_OPENFGA_STORE_ID"
ENV_MODEL_ID = "EAP_OPENFGA_MODEL_ID"
ENV_KEY = "EAP_OPENFGA_PRESHARED_KEY"
ENV_KEY_FALLBACK = "OPENFGA_PRESHARED_KEY"  # the name of the deploy Secret's field (deploy/k8s/iam/openfga.yaml)
ENV_TIMEOUT = "EAP_OPENFGA_TIMEOUT_SECONDS"
ENV_LIST_LIMIT = "EAP_OPENFGA_LIST_LIMIT"
ENV_BATCH_SIZE = "EAP_OPENFGA_BATCH_SIZE"

DEFAULT_TIMEOUT = 3.0
# OpenFGA's own defaults (OPENFGA_LIST_OBJECTS_MAX_RESULTS, OPENFGA_MAX_CHECKS_PER_BATCH_CHECK, OPENFGA_MAX_TUPLES_PER_WRITE).
DEFAULT_LIST_LIMIT = 1000
DEFAULT_BATCH_SIZE = 50
MAX_TUPLES_PER_WRITE = 100


@dataclass(frozen=True)
class OpenFgaSettings:
    url: str
    store_id: str
    model_id: str | None = None
    preshared_key: str | None = field(default=None, repr=False)  # never in a repr, a log or an error
    timeout: float = DEFAULT_TIMEOUT
    list_limit: int = DEFAULT_LIST_LIMIT
    batch_size: int = DEFAULT_BATCH_SIZE


def _positive(env: Mapping[str, str], name: str, default: float, cast: type) -> float | int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise IamConfigError(f"{name} must be a number") from None
    if value <= 0:
        raise IamConfigError(f"{name} must be positive")
    return value


def server_url(raw: str, name: str = ENV_URL) -> str:
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
        raise IamConfigError(f"{name} must be an http(s) URL with no query")
    return raw.rstrip("/")


def load_settings(env: Mapping[str, str]) -> OpenFgaSettings:
    """The settings, or IamConfigError naming what is missing (never a value)."""
    url = env.get(ENV_URL, "").strip()
    store = env.get(ENV_STORE_ID, "").strip()
    missing = [name for name, value in ((ENV_URL, url), (ENV_STORE_ID, store)) if not value]
    if missing:
        raise IamConfigError(f"the openfga plug-in needs {', '.join(missing)} (run `python -m eap_iam_openfga.bootstrap`)")
    return OpenFgaSettings(
        url=server_url(url),
        store_id=store,
        model_id=env.get(ENV_MODEL_ID, "").strip() or None,
        preshared_key=(env.get(ENV_KEY, "") or env.get(ENV_KEY_FALLBACK, "")).strip() or None,
        timeout=float(_positive(env, ENV_TIMEOUT, DEFAULT_TIMEOUT, float)),
        list_limit=int(_positive(env, ENV_LIST_LIMIT, DEFAULT_LIST_LIMIT, int)),
        batch_size=min(int(_positive(env, ENV_BATCH_SIZE, DEFAULT_BATCH_SIZE, int)), 100),
    )
