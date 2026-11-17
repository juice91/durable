"""Kubernetes documents for a managed MCP server, verified against the live ToolHive 0.65.0 CRD.

Verified facts (`kubectl get crd mcpservers.toolhive.stacklok.dev`, operator 0.65.0):

* Group `toolhive.stacklok.dev`. `v1beta1` is the storage version; `v1alpha1` is served but
  deprecated. We use `v1beta1`.
* Secret references: `spec.secrets[]` of `{name, key, targetEnvName}` (`name` and `key` required;
  `targetEnvName` defaults to `key`). `name` is a Kubernetes Secret in the CR's namespace. The
  operator injects it as `secretKeyRef` env on the MCP container. `spec.env[]` is `{name, value}`
  with both required, so it is only ever used for non-secret values.
* `spec.replicas` is the proxy runner Deployment, `spec.backendReplicas` the MCP server
  StatefulSet. Both 0 gives `status.phase: Stopped` ("scaled to zero").
* `status.phase` is one of Pending, Ready, Failed, Terminating, Stopped. `status.url` is
  `http://mcp-<cr>-proxy.<ns>.svc.cluster.local:8080/mcp`.
* Operator-created objects: Service `mcp-<cr>-proxy` (proxy port), Deployment `<cr>` (proxy),
  StatefulSet `<cr>` (backend, pod `<cr>-0`). Every pod carries `toolhive-name=<cr>`.
* `spec.resources` only reaches the proxy Deployment; limits for the MCP container go through
  `spec.podTemplateSpec.spec.containers[name=mcp]` (MR.0 spike).
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

GROUP = "toolhive.stacklok.dev"
VERSION = "v1beta1"
API_VERSION = f"{GROUP}/{VERSION}"
KIND = "MCPServer"
PLURAL = "mcpservers"

SECRETS_FIELD = "secrets"  # spec.secrets[] = {name, key, targetEnvName}
PHASES = ("Pending", "Ready", "Failed", "Terminating", "Stopped")
MCP_CONTAINER = "mcp"
PROXY_PORT = 8080
POD_LABEL = "toolhive-name"

MANAGED_BY = {"app.kubernetes.io/managed-by": "pydanticos-mcp-runtime"}


def namespace_name(tenant_id: str | uuid.UUID) -> str:
    return "mcp-tenant-" + hashlib.sha256(str(tenant_id).encode()).hexdigest()[:12]


def cr_name(server_id: str | uuid.UUID) -> str:
    return "s-" + str(server_id).replace("-", "").lower()[:12]


def secret_name(cr: str) -> str:
    return f"{cr}-env"


def proxy_service_name(cr: str) -> str:
    """The operator's proxy Service for a CR (verified: `mcp-<cr>-proxy`)."""
    return f"mcp-{cr}-proxy"


def endpoint_url(namespace: str, cr: str, port: int = PROXY_PORT) -> str:
    return f"http://{proxy_service_name(cr)}.{namespace}.svc:{port}/mcp"


def lightkube_mcpserver() -> Any:
    """The lightkube generic resource class for MCPServer (imported lazily)."""
    from lightkube.generic_resource import create_namespaced_resource

    return create_namespaced_resource(GROUP, VERSION, KIND, PLURAL)
