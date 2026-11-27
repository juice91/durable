"""The two tokens. The access token is a short-lived HS256 JWT naming a session; the refresh token is random and opaque,
stored only as an HMAC under the signing key, so rotating that key ends every session."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

import jwt

TOKEN_TYPE = "eap-localadmin+jwt"  # the header that says an access token is this plug-in's
ISSUER = "eap-localadmin"
AUDIENCE = "eap-api"
ACCESS_SECONDS = 900
MAX_TOKEN_BYTES = 4096
REFRESH_PREFIX = "lar"


def new_session_id() -> str:
    return secrets.token_hex(16)


def new_refresh_token(session_id: str) -> str:
    return f"{REFRESH_PREFIX}.{session_id}.{secrets.token_urlsafe(32)}"


def refresh_session_id(token: str) -> str | None:
    """The session a refresh token names, or None when it is not one (nothing is looked up then)."""
    if len(token) > 200 or not token.isascii():
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != REFRESH_PREFIX or len(parts[1]) != 32 or not parts[1].isalnum() or not parts[2]:
        return None
    return parts[1]


def new_setup_code() -> str:
    """160 random bits as 32 base32 characters (no 0, 1 or 8 to mistake for letters) in groups of four."""
    text = base64.b32encode(secrets.token_bytes(20)).decode()
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def setup_code_hash(code: str) -> str:
    """The digest stored for a code, whatever its case, spaces and dashes."""
    typed = "".join(ch for ch in code[:200].upper() if ch.isascii() and ch.isalnum())
    return hashlib.sha256(b"eap-localadmin-setup:" + typed.encode()).hexdigest()


def setup_code_line(code: str, studio_url: str | None = None) -> str:
    """The one line an operator greps for (`setup code`), ending with the setup link when Studio's address is known."""
    line = (
        f"eap localadmin setup code: {code}  (there is no admin account yet: enter this code on Studio's setup page; "
        "a restart or `python -m eap_iam_localadmin setup-code` replaces it)"
    )
    return f"{line}  Open {studio_url}/setup#code={code}" if studio_url else line


def refresh_hash(key: bytes, token: str) -> str:
    return hmac.new(key, b"eap-localadmin-refresh:" + token.encode(), hashlib.sha256).hexdigest()


def access_token(key: bytes, *, tenant_id: str, username: str, session_id: str, now: datetime) -> str:
    claims = {
        "iss": ISSUER, "aud": AUDIENCE, "sub": username, "ten": tenant_id, "sid": session_id,
        "iat": int(now.timestamp()), "exp": int((now + timedelta(seconds=ACCESS_SECONDS)).timestamp()),
    }
    return jwt.encode(claims, key, algorithm="HS256", headers={"typ": TOKEN_TYPE})


def is_ours(token: str) -> bool:
    if len(token) > MAX_TOKEN_BYTES or token.count(".") != 2:
        return False
    try:
        return jwt.get_unverified_header(token).get("typ") == TOKEN_TYPE
    except (jwt.PyJWTError, ValueError):
        return False


def verify_access(key: bytes, token: str, now: datetime) -> dict[str, str] | None:
    """The claims of a token this key signed that has not expired, else None. Expiry is judged by `now`."""
    try:
        claims = jwt.decode(
            token, key, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "sid", "ten"], "verify_exp": False, "verify_iat": False},
        )
    except (jwt.PyJWTError, ValueError):
        return None
    if not isinstance(claims["exp"], int) or claims["exp"] <= now.timestamp():
        return None
    if not all(isinstance(claims[name], str) for name in ("sub", "sid", "ten")):
        return None
    return claims
