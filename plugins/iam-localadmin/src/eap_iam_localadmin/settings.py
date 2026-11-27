"""`EAP_LOCAL_ADMIN_*` and `EAP_LOCAL_AUTH_*`. An error names the variable, never a value."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from eap_iam import IamConfigError

PROVIDER = "localadmin"
SUBJECT_PREFIX = "localadmin:"
USERNAME = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
USERNAME_RULE = "1 to 64 of the characters A-Z a-z 0-9 _ . @ -"
TENANT = re.compile(r"[A-Za-z0-9_-]{1,64}")
STUDIO_URL = re.compile(r"https?://[^\s?#]+")
DEFAULT_TENANT = "studio"
# 256 bits at least: 64 characters of hex, or 43 of base64 and the like.
MIN_HEX_CHARS = 64
MIN_KEY_CHARS = 43
PLACEHOLDER_WORDS = ("change", "example", "default", "secret", "placeholder", "xxx")
HOW_TO_MAKE_A_KEY = (
    "Generate one with `openssl rand -base64 48` and put it in the field EAP_LOCAL_AUTH_SIGNING_KEY of the Secret "
    "pydanticos-secrets (deploy/scripts/up.sh does this by itself). One command, then deploy or restart the API: "
    'kubectl -n pydanticos patch secret pydanticos-secrets -p "{\\"stringData\\":{\\"EAP_LOCAL_AUTH_SIGNING_KEY\\":'
    '\\"$(openssl rand -base64 48)\\"}}"'
)
DEFAULT_SESSION_HOURS = 12
MAX_SESSION_HOURS = 720


@dataclass(frozen=True)
class AccountSettings:
    tenant_id: str
    # With the initial password, makes the account at start; without both, Studio's setup page does.
    username: str | None
    # Where people reach Studio (EAP_STUDIO_URL): the printed setup code then comes with its link.
    studio_url: str | None = None


@dataclass(frozen=True)
class Settings(AccountSettings):
    signing_key: bytes = field(repr=False, default=b"")
    initial_password: str | None = field(repr=False, default=None)
    session_hours: int = DEFAULT_SESSION_HOURS


def load_account_settings(env: Mapping[str, str]) -> AccountSettings:
    username = env.get("EAP_LOCAL_ADMIN_USERNAME", "").strip() or None
    if username is not None and not USERNAME.fullmatch(username):
        raise IamConfigError(f"EAP_LOCAL_ADMIN_USERNAME must be {USERNAME_RULE}")
    tenant = env.get("EAP_LOCAL_ADMIN_TENANT", "").strip() or DEFAULT_TENANT
    if not TENANT.fullmatch(tenant):
        raise IamConfigError("EAP_LOCAL_ADMIN_TENANT must be 1 to 64 of the characters A-Z a-z 0-9 _ -")
    studio = env.get("EAP_STUDIO_URL", "").strip().rstrip("/") or None
    if studio is not None and not STUDIO_URL.fullmatch(studio):
        raise IamConfigError("EAP_STUDIO_URL must be an http or https address without spaces, a query or a fragment")
    return AccountSettings(tenant_id=tenant, username=username, studio_url=studio)


def signing_key_problem(key: str) -> str | None:
    """Why `key` may not sign tokens, or None. The answer never repeats the key."""
    if not key:
        return "is missing (the localadmin sign-in cannot start without it)"
    if len(key) < (MIN_HEX_CHARS if re.fullmatch(r"[0-9a-fA-F]+", key) else MIN_KEY_CHARS):
        return f"is too short: it needs {MIN_KEY_CHARS} random characters ({MIN_HEX_CHARS} if hexadecimal)"
    if len(set(key)) == 1 or any(word in key.lower() for word in PLACEHOLDER_WORDS):
        return "looks like a placeholder, not a random key"
    return None


def _key_from_file(env: Mapping[str, str]) -> str:
    """The key in the file EAP_LOCAL_AUTH_SIGNING_KEY_FILE names (a mounted secret), or "" when none is named."""
    path = env.get("EAP_LOCAL_AUTH_SIGNING_KEY_FILE", "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:  # name the class only: never the content
        raise IamConfigError(
            f"EAP_LOCAL_AUTH_SIGNING_KEY_FILE cannot be read ({type(exc).__name__}). {HOW_TO_MAKE_A_KEY}"
        ) from None


def load_settings(env: Mapping[str, str]) -> Settings:
    account = load_account_settings(env)
    key = env.get("EAP_LOCAL_AUTH_SIGNING_KEY", "").strip() or _key_from_file(env)
    problem = signing_key_problem(key)
    if problem:
        raise IamConfigError(f"EAP_LOCAL_AUTH_SIGNING_KEY {problem}. {HOW_TO_MAKE_A_KEY}")
    raw = env.get("EAP_LOCAL_AUTH_SESSION_HOURS", "").strip()
    try:
        hours = int(raw) if raw else DEFAULT_SESSION_HOURS
    except ValueError:
        hours = 0
    if not 1 <= hours <= MAX_SESSION_HOURS:
        raise IamConfigError(f"EAP_LOCAL_AUTH_SESSION_HOURS must be a whole number from 1 to {MAX_SESSION_HOURS}")
    return Settings(
        tenant_id=account.tenant_id,
        username=account.username,
        studio_url=account.studio_url,
        signing_key=key.encode(),
        initial_password=env.get("EAP_LOCAL_ADMIN_INITIAL_PASSWORD") or None,
        session_hours=hours,
    )
