"""The MCP control plane's tool declarations, as the capability registry reads them.

`eap_api.mcp_control.server.register` records every tool it registers here (name, scope, description,
`human_only`), so the registry lists exactly the tools the endpoint serves and nothing keeps a second list. This
module imports no entry point: services and providers read it without reaching into `mcp_control`.
"""

from __future__ import annotations

from eap_schemas.capabilities import ControlToolDecl

_DECLS: dict[str, ControlToolDecl] = {}


def record(
    name: str, *, manage: bool = False, description: str | None = None, human_only: bool = False, internal: bool = False
) -> None:
    """Remember one registered control tool (registering the same name again replaces it)."""
    _DECLS[name] = ControlToolDecl(
        name=name, description=(description or "").strip(), manage=manage, human_only=human_only, internal=internal
    )


def declared() -> tuple[ControlToolDecl, ...]:
    """The control tools registered so far, sorted by name."""
    return tuple(sorted(_DECLS.values(), key=lambda d: d.name))
