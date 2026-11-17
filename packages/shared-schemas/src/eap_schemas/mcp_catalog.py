"""Contracts for the MCP catalog (a mirror of the official registry) and managed runtimes."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RegistryType = Literal["npm", "pypi", "oci"]
PackageTransport = Literal["stdio", "streamable-http"]
RuntimeState = Literal["pending", "starting", "running", "idle", "stopped", "failed"]
RUNTIME_STATES: tuple[str, ...] = ("pending", "starting", "running", "idle", "stopped", "failed")


class EnvVarSpec(BaseModel):
    """An environment variable a package reads. `secret` values are sealed, never shown again."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    required: bool = False
    secret: bool = False


class CatalogPackage(BaseModel):
    """One runnable package of a catalog entry."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    registry_type: RegistryType
    identifier: str = Field(min_length=1)
    version: str | None = None
    transport: PackageTransport = "stdio"
    environment_variables: tuple[EnvVarSpec, ...] = ()


CatalogSource = Literal["official", "toolhive"]


class CatalogNetwork(BaseModel):
    """A publisher's declared outbound network permissions (ToolHive `permissions.network.outbound`)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    allow_host: tuple[str, ...] = ()
    allow_port: tuple[int, ...] = ()
    insecure_allow_all: bool = False


class CatalogMeta(BaseModel):
    """The publisher-provided block we keep for a ToolHive entry."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    tier: str | None = None
    tags: tuple[str, ...] = ()
    network: CatalogNetwork | None = None
    tools: tuple[str, ...] = ()
    tool_count: int = 0
    stars: int | None = None


class CatalogEntry(BaseModel):
    """A mirrored registry entry (platform-wide, read-only for tenants)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = Field(min_length=1)
    title: str | None = None
    description: str | None = None
    version: str
    status: str = "active"
    is_latest: bool = True
    packages: tuple[CatalogPackage, ...] = ()
    remotes: tuple[dict, ...] = ()
    licence: str | None = None
    fetched_at: datetime | None = None
    hidden: bool = False
    source: CatalogSource = "official"
    meta: CatalogMeta | None = None


__all__ = ["CatalogEntry", "CatalogMeta", "CatalogNetwork", "CatalogSource", "CatalogPackage", "EnvVarSpec", "PackageTransport", "RegistryType", "RUNTIME_STATES", "RuntimeState"]
