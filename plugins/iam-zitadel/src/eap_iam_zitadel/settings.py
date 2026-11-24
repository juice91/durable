"""Configuration of the ZITADEL plug-in, read from `EAP_ZITADEL_*` (design 1.7).

Not configured at all (no project id and no org map) is *dormant*: the identity provider recognises nothing and the
chain moves on, so a deploy can list `zitadel` before the operator has run the bootstrap. Half configured (one of the
two set, or no issuer) refuses to start."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from eap_iam import IamConfigError

ENV_ISSUER = "EAP_ZITADEL_ISSUER"
ENV_PROJECT_ID = "EAP_ZITADEL_PROJECT_ID"
ENV_ORG_TENANT_MAP = "EAP_ZITADEL_ORG_TENANT_MAP"
ENV_API_URL = "EAP_ZITADEL_API_URL"
ENV_MACHINE_KEY = "EAP_ZITADEL_MACHINE_KEY"


@dataclass(frozen=True)
class ServiceKey:
    """The machine user's key (the JSON ZITADEL issues for a JWT-profile key). Never logged, never repr'd."""

    user_id: str
    key_id: str
    private_key: str

    def __repr__(self) -> str:  # a stray log of the settings must not leak the key
        return f"ServiceKey(user_id={self.user_id!r}, key_id={self.key_id!r}, private_key=<hidden>)"


@dataclass(frozen=True)
class ZitadelSettings:
    issuer: str  # exactly the `iss` of the tokens, no trailing slash
    project_id: str  # the project whose id must be in `aud`
    org_tenants: Mapping[str, str]  # ZITADEL org id -> platform tenant id
    api_url: str  # where this process reaches ZITADEL (in-cluster Service); the `Host` header is the issuer's host
    machine_key: ServiceKey | None
    jwks_ttl: float = 3600.0
    jwks_min_refresh: float = 30.0
    jwks_grace: float = 86400.0

    @property
    def issuer_host(self) -> str:
        return urlsplit(self.issuer).netloc

    def org_for_tenant(self, tenant_id: str) -> str | None:
        for org, tenant in self.org_tenants.items():
            if tenant == tenant_id:
                return org
        return None

    @property
    def jwks_url(self) -> str:
        return f"{self.api_url}/oauth/v2/keys"


def parse_org_tenant_map(raw: str) -> dict[str, str]:
    """`<org id>=<tenant>,<org id>=<tenant>`; a JSON object `{"<org id>": "<tenant>"}` is accepted too."""
    raw = raw.strip()
    if not raw:
        return {}
    pairs: list[tuple[str, str]] = []
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            raise IamConfigError(f"{ENV_ORG_TENANT_MAP} is not valid JSON") from None
        if not isinstance(data, dict):
            raise IamConfigError(f"{ENV_ORG_TENANT_MAP} must be an object or <org id>=<tenant> pairs")
        pairs = [(str(k).strip(), str(v).strip()) for k, v in data.items()]
    else:
        for part in raw.split(","):
            if not part.strip():
                continue
            org, sep, tenant = part.partition("=")
            if not sep:
                raise IamConfigError(f"{ENV_ORG_TENANT_MAP} entries are <org id>=<tenant>; got {part.strip()!r}")
            pairs.append((org.strip(), tenant.strip()))
    mapping: dict[str, str] = {}
    for org, tenant in pairs:
        if not org or not tenant:
            raise IamConfigError(f"{ENV_ORG_TENANT_MAP} has an empty org id or tenant")
        if org in mapping:
            raise IamConfigError(f"{ENV_ORG_TENANT_MAP} lists org {org} twice")
        mapping[org] = tenant
    if len(set(mapping.values())) != len(mapping):
        raise IamConfigError(f"{ENV_ORG_TENANT_MAP}: a tenant maps to one organisation only")
    return mapping


def parse_machine_key(raw: str) -> ServiceKey:
    try:
        data = json.loads(raw)
        user_id, key_id, private_key = data["userId"], data["keyId"], data["key"]
    except (ValueError, KeyError, TypeError):
        # Never echo the value: it is a private key.
        raise IamConfigError(f"{ENV_MACHINE_KEY} is not a ZITADEL machine-user key (JSON with userId, keyId, key)") from None
    if not all(isinstance(v, str) and v for v in (user_id, key_id, private_key)):
        raise IamConfigError(f"{ENV_MACHINE_KEY} is not a ZITADEL machine-user key (JSON with userId, keyId, key)")
    return ServiceKey(user_id=user_id, key_id=key_id, private_key=private_key)


def _https_or_loopback(url: str, name: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
        raise IamConfigError(f"{name} must be an http(s) URL with no query")
    return url.rstrip("/")


def load_settings(env: Mapping[str, str]) -> ZitadelSettings | None:
    """The settings, or None when the plug-in is dormant. IamConfigError when half configured."""
    project = env.get(ENV_PROJECT_ID, "").strip()
    org_map_raw = env.get(ENV_ORG_TENANT_MAP, "").strip()
    if not project and not org_map_raw:
        return None
    missing = [name for name, value in ((ENV_PROJECT_ID, project), (ENV_ORG_TENANT_MAP, org_map_raw)) if not value]
    issuer = env.get(ENV_ISSUER, "").strip()
    if not issuer:
        missing.append(ENV_ISSUER)
    if missing:
        raise IamConfigError(f"the zitadel plug-in is half configured; it also needs {', '.join(missing)}")
    issuer = _https_or_loopback(issuer, ENV_ISSUER)
    api_url = _https_or_loopback(env.get(ENV_API_URL, "").strip() or issuer, ENV_API_URL)
    key_raw = env.get(ENV_MACHINE_KEY, "").strip()
    return ZitadelSettings(
        issuer=issuer,
        project_id=project,
        org_tenants=parse_org_tenant_map(org_map_raw),
        api_url=api_url,
        machine_key=parse_machine_key(key_raw) if key_raw else None,
        jwks_ttl=float(env.get("EAP_ZITADEL_JWKS_TTL_SECONDS", "3600")),
        jwks_min_refresh=float(env.get("EAP_ZITADEL_JWKS_MIN_REFRESH_SECONDS", "30")),
        jwks_grace=float(env.get("EAP_ZITADEL_JWKS_GRACE_SECONDS", "86400")),
    )
