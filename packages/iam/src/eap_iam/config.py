"""Start-up rules (design 5.1): `require_iam_configured` replaces `require_auth_configured`. Same shape: explicit or
refuse. `EAP_AUTH_MODE` stays the master switch; `EAP_IAM_IDENTITY`, `EAP_IAM_POLICY`, `EAP_IAM_MEMBERSHIP` select
plug-ins. Nothing here imports a plug-in: it only checks names against what the registry found."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from eap_iam.registry import parse_names
from eap_iam.types import IamConfigError

MODE_DEV = "dev"
MODE_PRODUCTION = "production"
MODE_API_KEY = "api_key"  # deprecated alias for one release: production with the apikey plug-in, AG-UI keys only
MODES = (MODE_DEV, MODE_PRODUCTION, MODE_API_KEY)

DEV_DEFAULT_IDENTITY = ("apikey", "devloopback")
DEV_DEFAULT_POLICY = "openaccess"
API_KEY_ALIAS_IDENTITY = ("apikey",)
# The identity plug-in of inbound webhook signatures. It answers only the hook route (the one route that accepts the
# `webhook` principal kind), so it is part of the chain whenever it is installed, whatever EAP_IAM_IDENTITY lists.
WEBHOOK_IDENTITY = "webhook"

AUTH_CONFIGURATION_ERROR = (
    "no production authentication is configured; set EAP_AUTH_MODE=dev for local development only "
    "(it defaults EAP_IAM_IDENTITY=apikey,devloopback and EAP_IAM_POLICY=openaccess), or EAP_AUTH_MODE=production "
    "with EAP_IAM_IDENTITY (at least one identity provider) and EAP_IAM_POLICY (exactly one policy engine). "
    "EAP_AUTH_MODE=api_key is a deprecated alias that serves only the AG-UI routes (/api/v1/ag-ui/*) to tenant API keys"
)


@dataclass(frozen=True)
class IamSettings:
    mode: str
    identity: tuple[str, ...]
    policy: str
    membership: str | None
    allow_transitional: bool


def auth_mode(env: Mapping[str, str]) -> str | None:
    mode = env.get("EAP_AUTH_MODE")
    return mode if mode in MODES else None


def require_iam_configured(
    env: Mapping[str, str],
    *,
    identity: Mapping[str, object],
    policy: Mapping[str, object],
    membership: Mapping[str, object],
) -> IamSettings:
    """The validated plug-in selection, or IamConfigError (a RuntimeError) naming what is wrong.

    `identity`, `policy` and `membership` map the installed plug-in names to anything (only the names are used)."""
    mode = auth_mode(env)
    if mode is None:
        raise IamConfigError(AUTH_CONFIGURATION_ERROR)

    identity_names = parse_names(env.get("EAP_IAM_IDENTITY"))
    policy_names = parse_names(env.get("EAP_IAM_POLICY"))
    membership_names = parse_names(env.get("EAP_IAM_MEMBERSHIP"))
    allow_transitional = env.get("EAP_IAM_ALLOW_TRANSITIONAL") == "1"

    if mode == MODE_DEV:
        if not identity_names:
            identity_names = list(DEV_DEFAULT_IDENTITY)
        if not policy_names:
            policy_names = [DEV_DEFAULT_POLICY]
    elif mode == MODE_API_KEY:
        if not identity_names:
            identity_names = list(API_KEY_ALIAS_IDENTITY)
        if not policy_names:
            policy_names = [DEV_DEFAULT_POLICY]
        allow_transitional = True  # the legacy mode had no policy engine at all: nobody decided anything
    elif not identity_names:
        raise IamConfigError(
            "EAP_AUTH_MODE=production needs EAP_IAM_IDENTITY with at least one identity provider " + AUTH_CONFIGURATION_ERROR
        )

    if WEBHOOK_IDENTITY in identity and WEBHOOK_IDENTITY not in identity_names:
        identity_names.append(WEBHOOK_IDENTITY)
    if len(set(identity_names)) != len(identity_names):
        raise IamConfigError(f"EAP_IAM_IDENTITY lists a plug-in twice: {','.join(identity_names)}")
    unknown = [n for n in identity_names if n not in identity]
    if unknown:
        raise IamConfigError(
            f"EAP_IAM_IDENTITY names plug-ins that are not installed: {', '.join(unknown)} "
            f"(installed: {', '.join(sorted(identity)) or 'none'})"
        )
    if len(policy_names) != 1:
        raise IamConfigError(
            "EAP_IAM_POLICY must name exactly one policy engine "
            f"(got {','.join(policy_names) or 'none'}; installed: {', '.join(sorted(policy)) or 'none'})"
        )
    if policy_names[0] not in policy:
        raise IamConfigError(
            f"EAP_IAM_POLICY names a plug-in that is not installed: {policy_names[0]} "
            f"(installed: {', '.join(sorted(policy)) or 'none'})"
        )
    if len(membership_names) > 1:
        raise IamConfigError("EAP_IAM_MEMBERSHIP names at most one membership source")
    if membership_names and membership_names[0] not in membership:
        raise IamConfigError(f"EAP_IAM_MEMBERSHIP names a plug-in that is not installed: {membership_names[0]}")
    if "devloopback" in identity_names and "*" in env.get("FORWARDED_ALLOW_IPS", ""):
        raise IamConfigError(
            "the dev bearer trusts the TCP peer address only: refusing to start with FORWARDED_ALLOW_IPS=* "
            "(proxy headers would rewrite the peer); run uvicorn with --no-proxy-headers"
        )
    return IamSettings(
        mode=mode,
        identity=tuple(identity_names),
        policy=policy_names[0],
        membership=membership_names[0] if membership_names else None,
        allow_transitional=allow_transitional,
    )
