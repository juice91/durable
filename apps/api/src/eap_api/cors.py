"""CORS for the API: Studio everywhere, plus a per-deployment allowlist on `/api/v1/ag-ui/*` (AGUI.9).

D17 of the AG-UI plan. The recommended AG-UI topology is server-side (a CopilotKit runtime or a
backend holds the API key, and no browser talks to us). For browser-direct prototypes,
`EAP_AGUI_CORS_ORIGINS` (comma-separated origins, default empty) is allowed on the AG-UI routes
**only**: a preflight from such an origin to any other route gets no CORS headers, so the browser
blocks it. Studio's own origins keep working everywhere, the AG-UI routes included (the Chat tab
dogfoods them).

The AG-UI policy sends no credentials (keys travel in `Authorization`, never cookies), allows only
GET and POST with the headers an AG-UI client sends, and exposes only `Retry-After` (the 429
backoff of `ResumableHttpAgent`). The per-key `allowed_origins` check is not here: it runs after
authentication, in `eap_api.auth.agui_principal`.
"""

from __future__ import annotations

import os

from eap_run_store import normalize_origin
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

AGUI_CORS_ORIGINS_ENV = "EAP_AGUI_CORS_ORIGINS"
# The local Studio dev server, by either loopback name.
STUDIO_ORIGINS: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")
AGUI_PATH_PREFIX = "/api/v1/ag-ui"
AGUI_ALLOWED_METHODS: tuple[str, ...] = ("GET", "POST")
AGUI_ALLOWED_HEADERS: tuple[str, ...] = ("Authorization", "Content-Type", "Accept", "Last-Event-ID")
AGUI_EXPOSED_HEADERS: tuple[str, ...] = ("Retry-After",)
AGUI_PREFLIGHT_MAX_AGE_S = 600


class CorsConfigurationError(RuntimeError):
    """A malformed EAP_AGUI_CORS_ORIGINS: the API refuses to start rather than guess."""

    def __init__(self, entry: str, reason: str) -> None:
        super().__init__(entry, reason)
        self.entry = entry
        self.reason = reason

    def __str__(self) -> str:
        return f"{AGUI_CORS_ORIGINS_ENV}: {self.entry!r} {self.reason}"


def agui_cors_origins_from_env(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """The extra browser origins allowed on `/api/v1/ag-ui/*`, normalized like a key's
    `allowed_origins` (`eap_run_store.normalize_origin`), de-duplicated, in order.

    Unset or blank is none. `*` is refused (the allowlist is per deployment, D17), and so is
    anything that is not `scheme://host[:port]`."""
    raw = (environ if environ is not None else os.environ).get(AGUI_CORS_ORIGINS_ENV, "")
    origins: list[str] = []
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        if entry == "*":
            raise CorsConfigurationError(entry, "is not allowed; list each origin")
        try:
            origin = normalize_origin(entry)
        except ValueError:
            raise CorsConfigurationError(entry, "is not an origin (scheme://host[:port])") from None
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


def is_agui_path(path: str) -> bool:
    return path == AGUI_PATH_PREFIX or path.startswith(AGUI_PATH_PREFIX + "/")


class PathScopedCORSMiddleware:
    """Two CORS policies chosen by path: the AG-UI one on `/api/v1/ag-ui/*`, Studio's elsewhere."""

    def __init__(self, app: ASGIApp, *, agui_origins: tuple[str, ...] = ()) -> None:
        self.agui_origins = tuple(dict.fromkeys((*STUDIO_ORIGINS, *agui_origins)))
        self._default = CORSMiddleware(
            app,
            allow_origins=list(STUDIO_ORIGINS),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self._agui = CORSMiddleware(
            app,
            allow_origins=list(self.agui_origins),
            allow_credentials=False,
            allow_methods=list(AGUI_ALLOWED_METHODS),
            allow_headers=list(AGUI_ALLOWED_HEADERS),
            expose_headers=list(AGUI_EXPOSED_HEADERS),
            max_age=AGUI_PREFLIGHT_MAX_AGE_S,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_agui_path(scope.get("path", "")):
            await self._agui(scope, receive, send)
        else:
            await self._default(scope, receive, send)
