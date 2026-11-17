"""`OperatorRuntime`: MCPServer CR, Secret, NetworkPolicy and ResourceQuota per tenant namespace."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Sequence
from typing import Any, Protocol

from eap_mcp_runtime import k8s_models as m
from eap_mcp_runtime.k8s_models import MANAGED_BY
from eap_mcp_runtime.models import RuntimeHandle, RuntimeSpec, RuntimeStatus

FIELD_MANAGER = "pydanticos-mcp-runtime"
PRIVATE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10")
STATE_BY_PHASE = {
    "Pending": "starting",
    "Ready": "running",
    "Failed": "failed",
    "Terminating": "stopping",
    "Stopped": "stopped",
}


def handle_for(tenant_id: str, server_id: str) -> RuntimeHandle:
    return RuntimeHandle(m.namespace_name(tenant_id), m.cr_name(server_id))


# --- documents (pure; unit tested) -----------------------------------------------------------


def namespace_doc(namespace: str, tenant_id: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {**MANAGED_BY, "pydanticos.io/mcp-tenant": hashlib.sha256(tenant_id.encode()).hexdigest()[:12]},
        },
    }


def quota_doc(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "mcp-quota", "namespace": namespace, "labels": MANAGED_BY},
        "spec": {
            "hard": {
                "pods": "20",
                "limits.cpu": "8",
                "limits.memory": "8Gi",
                "requests.cpu": "4",
                "requests.memory": "4Gi",
                "secrets": "40",
            }
        },
    }


def limit_range_doc(namespace: str) -> dict[str, Any]:
    """Defaults for any container the operator creates without limits (the quota requires them)."""
    return {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": {"name": "mcp-defaults", "namespace": namespace, "labels": MANAGED_BY},
        "spec": {
            "limits": [
                {
                    "type": "Container",
                    "default": {"cpu": "250m", "memory": "128Mi"},
                    "defaultRequest": {"cpu": "25m", "memory": "32Mi"},
                }
            ]
        },
    }


def secret_doc(namespace: str, cr: str, values: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": m.secret_name(cr), "namespace": namespace, "labels": {**MANAGED_BY, m.POD_LABEL: cr}},
        "type": "Opaque",
        "stringData": dict(values),
    }


def network_policy_doc(
    namespace: str,
    cr: str,
    *,
    platform_namespace: str,
    worker_label: dict[str, str],
    api_server_cidrs: Sequence[str],
) -> dict[str, Any]:
    own = {"podSelector": {"matchLabels": {m.POD_LABEL: cr}}}
    egress: list[dict[str, Any]] = [
        {"to": [own]},
        {
            "to": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }
            ],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        },
        {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(PRIVATE_RANGES)}}]},
    ]
    if api_server_cidrs:  # the proxy runner attaches to the backend through the API server
        egress.append({"to": [{"ipBlock": {"cidr": c}} for c in api_server_cidrs]})
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": f"{cr}-isolation", "namespace": namespace, "labels": {**MANAGED_BY, m.POD_LABEL: cr}},
        "spec": {
            "podSelector": {"matchLabels": {m.POD_LABEL: cr}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {"from": [own]},
                {
                    "from": [
                        {
                            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": platform_namespace}},
                            "podSelector": {"matchLabels": worker_label},
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": m.PROXY_PORT}],
                },
            ],
            "egress": egress,
        },
    }


def mcpserver_doc(
    namespace: str,
    cr: str,
    *,
    image: str,
    args: Sequence[str],
    env: dict[str, str],
    secret_names: Sequence[str],
    transport: str,
    port: int | None,
    display_name: str,
    tenant_id: str,
    server_id: str,
    replicas: int = 1,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "image": image,
        "transport": transport,
        "proxyPort": m.PROXY_PORT,
        "replicas": replicas,
        "backendReplicas": replicas,
        "resources": {  # the proxy runner Deployment; the MCP container is limited below
            "requests": {"cpu": "25m", "memory": "32Mi"},
            "limits": {"cpu": "250m", "memory": "128Mi"},
        },
        "podTemplateSpec": {
            "spec": {
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [
                    {
                        "name": m.MCP_CONTAINER,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "500m", "memory": "256Mi"},
                        },
                        "volumeMounts": [{"name": "work", "mountPath": "/tmp"}],
                    }
                ],
                "volumes": [{"name": "work", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}}],
            }
        },
    }
    if args:
        spec["args"] = list(args)
    if transport != "stdio" and port:
        spec["mcpPort"] = port
    if env:  # non-secret values only
        spec["env"] = [{"name": k, "value": v} for k, v in env.items()]
    if secret_names:  # references only: Secret `<cr>-env`, key == env var name
        spec[m.SECRETS_FIELD] = [
            {"name": m.secret_name(cr), "key": k, "targetEnvName": k} for k in secret_names
        ]
    return {
        "apiVersion": m.API_VERSION,
        "kind": m.KIND,
        "metadata": {
            "name": cr,
            "namespace": namespace,
            "labels": MANAGED_BY,
            "annotations": {
                "pydanticos.io/display-name": display_name,
                "pydanticos.io/tenant-id": tenant_id,
                "pydanticos.io/server-id": server_id,
            },
        },
        "spec": spec,
    }


# --- Kubernetes access ------------------------------------------------------------------------


class KubeApi(Protocol):
    async def apply(self, doc: dict[str, Any]) -> None: ...

    async def get(self, kind: str, namespace: str | None, name: str) -> dict[str, Any] | None: ...

    async def delete(self, kind: str, namespace: str | None, name: str) -> None:
        """Delete; a missing object is not an error."""

    async def merge_patch(self, kind: str, namespace: str, name: str, body: dict[str, Any]) -> None: ...

    async def pod_names(self, namespace: str, label_selector: str) -> list[str]: ...

    async def pod_log(self, namespace: str, pod: str, tail: int) -> str: ...


class LightkubeApi:
    """`KubeApi` on lightkube's async client."""

    def __init__(self, client: Any | None = None, *, config: Any | None = None) -> None:
        from lightkube import AsyncClient

        self._client = client or AsyncClient(config=config, field_manager=FIELD_MANAGER)
        self._mcpserver = m.lightkube_mcpserver()

    def _cls(self, kind: str) -> Any:
        from lightkube.resources.core_v1 import LimitRange, Namespace, Pod, ResourceQuota, Secret
        from lightkube.resources.networking_v1 import NetworkPolicy

        return {
            "Namespace": Namespace,
            "Secret": Secret,
            "ResourceQuota": ResourceQuota,
            "LimitRange": LimitRange,
            "NetworkPolicy": NetworkPolicy,
            "Pod": Pod,
            m.KIND: self._mcpserver,
        }[kind]

    async def apply(self, doc: dict[str, Any]) -> None:
        cls = self._cls(doc["kind"])
        obj = cls.from_dict(doc) if hasattr(cls, "from_dict") else cls(**doc)
        await self._client.apply(obj, field_manager=FIELD_MANAGER, force=True)

    async def get(self, kind: str, namespace: str | None, name: str) -> dict[str, Any] | None:
        from lightkube import ApiError

        try:
            obj = await self._client.get(self._cls(kind), name, namespace=namespace)
        except ApiError as e:
            if e.status.code == 404:
                return None
            raise
        return _to_dict(obj)

    async def delete(self, kind: str, namespace: str | None, name: str) -> None:
        from lightkube import ApiError

        try:
            await self._client.delete(self._cls(kind), name, namespace=namespace)
        except ApiError as e:
            if e.status.code != 404:
                raise

    async def merge_patch(self, kind: str, namespace: str, name: str, body: dict[str, Any]) -> None:
        from lightkube import types

        await self._client.patch(self._cls(kind), name, body, namespace=namespace, patch_type=types.PatchType.MERGE)

    async def pod_names(self, namespace: str, label_selector: str) -> list[str]:
        labels = dict(p.split("=", 1) for p in label_selector.split(","))
        return [
            pod.metadata.name
            async for pod in self._client.list(self._cls("Pod"), namespace=namespace, labels=labels)
        ]

    async def pod_log(self, namespace: str, pod: str, tail: int) -> str:
        lines = [
            line
            async for line in self._client.log(pod, namespace=namespace, container=m.MCP_CONTAINER, tail_lines=tail)
        ]
        return "\n".join(lines)


def _to_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    from lightkube.core.dataclasses_dict import DataclassDictMixIn

    if isinstance(obj, DataclassDictMixIn):
        return obj.to_dict()
    return dict(obj)


# --- the adapter -----------------------------------------------------------------------------


class OperatorRuntime:
    def __init__(
        self,
        api: KubeApi | None = None,
        *,
        platform_namespace: str = "pydanticos",
        worker_label: dict[str, str] | None = None,
        api_server_cidrs: Sequence[str] = (),
    ) -> None:
        self._api = api or LightkubeApi()
        self._platform_namespace = platform_namespace
        self._worker_label = worker_label or {"app.kubernetes.io/name": "worker"}
        self._api_server_cidrs = tuple(api_server_cidrs)

    async def start(self, spec: RuntimeSpec) -> RuntimeHandle:
        h = RuntimeHandle(m.namespace_name(spec.tenant_id), m.cr_name(spec.server_id))
        await self._api.apply(namespace_doc(h.namespace, spec.tenant_id))
        await self._api.apply(limit_range_doc(h.namespace))
        await self._api.apply(quota_doc(h.namespace))
        if spec.secret_refs:
            await self._api.apply(secret_doc(h.namespace, h.cr_name, dict(spec.secret_refs)))
        await self._api.apply(
            network_policy_doc(
                h.namespace,
                h.cr_name,
                platform_namespace=self._platform_namespace,
                worker_label=self._worker_label,
                api_server_cidrs=self._api_server_cidrs,
            )
        )
        await self._api.apply(
            mcpserver_doc(
                h.namespace,
                h.cr_name,
                image=spec.image,
                args=spec.args,
                env=dict(spec.env),
                secret_names=list(spec.secret_refs),
                transport=spec.transport,
                port=spec.port,
                display_name=spec.name,
                tenant_id=spec.tenant_id,
                server_id=spec.server_id,
            )
        )
        return h

    async def stop(self, handle: RuntimeHandle) -> None:
        if await self._api.get(m.KIND, handle.namespace, handle.cr_name) is None:
            return
        await self._api.merge_patch(
            m.KIND, handle.namespace, handle.cr_name, {"spec": {"replicas": 0, "backendReplicas": 0}}
        )

    async def delete(self, handle: RuntimeHandle) -> None:
        # Idempotent: each step ignores a missing object, so a crash midway is repaired by re-running.
        await self._api.delete(m.KIND, handle.namespace, handle.cr_name)
        await self._api.delete("Secret", handle.namespace, m.secret_name(handle.cr_name))
        await self._api.delete("NetworkPolicy", handle.namespace, f"{handle.cr_name}-isolation")

    async def status(self, handle: RuntimeHandle) -> RuntimeStatus:
        endpoint = self.endpoint(handle)
        cr = await self._api.get(m.KIND, handle.namespace, handle.cr_name)
        if cr is None:
            return RuntimeStatus("absent", endpoint, "not found")
        st = cr.get("status") or {}
        state = STATE_BY_PHASE.get(st.get("phase", ""), "starting")
        return RuntimeStatus(state, endpoint, st.get("message", ""))  # type: ignore[arg-type]

    async def logs(self, handle: RuntimeHandle, tail: int = 200) -> list[str]:
        pods = await self._api.pod_names(handle.namespace, f"{m.POD_LABEL}={handle.cr_name}")
        backend = f"{handle.cr_name}-0"
        if backend not in pods:
            return []
        text = await self._api.pod_log(handle.namespace, backend, tail)
        return text.splitlines()

    def endpoint(self, handle: RuntimeHandle) -> str:
        return m.endpoint_url(handle.namespace, handle.cr_name)

    async def wait_for(self, handle: RuntimeHandle, state: str, timeout: float = 180.0) -> RuntimeStatus:
        deadline = time.monotonic() + timeout
        while True:
            s = await self.status(handle)
            if s.state == state or time.monotonic() >= deadline:
                return s
            await asyncio.sleep(2)

