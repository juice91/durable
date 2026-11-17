"""The `McpRuntime` contract: spec, handle, status and protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

RuntimeState = Literal["absent", "starting", "running", "stopping", "stopped", "failed"]


@dataclass(frozen=True)
class RuntimeSpec:
    """What to run. `env` holds non-secret values; `secret_refs` maps an environment variable name
    to its secret value, which the adapter writes only into a Kubernetes Secret (the value never
    appears in the MCPServer CR, and the field is hidden from `repr`)."""

    tenant_id: str
    server_id: str
    name: str
    image: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    secret_refs: Mapping[str, str] = field(default_factory=dict, repr=False)
    transport: Literal["stdio", "streamable-http", "sse"] = "stdio"
    port: int | None = None  # the port the server listens on (non-stdio transports)

    @property
    def env_names(self) -> tuple[str, ...]:
        return (*self.env, *self.secret_refs)


@dataclass(frozen=True)
class RuntimeHandle:
    namespace: str
    cr_name: str


@dataclass(frozen=True)
class RuntimeStatus:
    state: RuntimeState
    endpoint: str
    message: str = ""


class McpRuntime(Protocol):
    async def start(self, spec: RuntimeSpec) -> RuntimeHandle: ...

    async def stop(self, handle: RuntimeHandle) -> None: ...

    async def delete(self, handle: RuntimeHandle) -> None: ...

    async def status(self, handle: RuntimeHandle) -> RuntimeStatus: ...

    async def logs(self, handle: RuntimeHandle, tail: int = 200) -> list[str]: ...

    def endpoint(self, handle: RuntimeHandle) -> str: ...
