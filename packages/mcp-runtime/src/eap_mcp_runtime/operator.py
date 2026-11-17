"""`OperatorRuntime`: MCPServer CR, Secret, NetworkPolicy and ResourceQuota per tenant namespace."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import httpx2

from eap_mcp_runtime import k8s_models as m
from eap_mcp_runtime.k8s_models import MANAGED_BY
from eap_mcp_runtime.models import RuntimeHandle, RuntimeSpec, RuntimeStatus

log = logging.getLogger("eap_mcp_runtime.operator")
FIELD_MANAGER = "pydanticos-mcp-runtime"
# Excluded from the pod's public egress: private, link-local, CGNAT, loopback, "this network",
# IETF protocol assignments, benchmarking, multicast and reserved space.
PRIVATE_RANGES = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10",
    "0.0.0.0/8", "127.0.0.0/8", "192.0.0.0/24", "198.18.0.0/15", "224.0.0.0/3",
)
API_SERVER_PORTS_ENV = "EAP_MCP_API_SERVER_PORTS"
# The Service port (443) and the endpoint port the packet has after Service NAT: 6443 on stock
# Kubernetes, but not everywhere (colima's k3s listens on 57345). up.sh reads the real one.
DEFAULT_API_SERVER_PORTS = (443, 6443)
WORKER_SA_ENV = "EAP_MCP_WORKER_SERVICE_ACCOUNT"
API_SA_ENV = "EAP_MCP_API_SERVICE_ACCOUNT"
TENANT_ROLE = "pydanticos-mcp-tenant"  # ClusterRoles in deploy/k8s/rbac-mcp.yaml
TENANT_READ_ROLE = "pydanticos-mcp-tenant-read"
DEFAULT_CALLER_LABELS = ({"app.kubernetes.io/name": "worker"}, {"app.kubernetes.io/name": "api"})
POD_TMP_ENV = "EAP_MCP_POD_TMP_SIZE"
DEFAULT_POD_TMP = "512Mi"
POD_CPU_ENV = "EAP_MCP_POD_CPU"
POD_MEMORY_ENV = "EAP_MCP_POD_MEMORY"
API_SERVER_CIDRS_ENV = "EAP_MCP_API_SERVER_CIDRS"
DEFAULT_POD_CPU = "1"
DEFAULT_POD_MEMORY = "1Gi"
REQUEST_CPU = "50m"
REQUEST_MEMORY = "64Mi"
# Container waiting/terminated reasons that will not clear by waiting: the operator keeps the phase
# at Pending while the pod crash-loops or cannot pull its image.
FAILED_REASONS = frozenset(
    {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "OOMKilled"}
)
CALL_TIMEOUT = 45.0  # bound for one Kubernetes API call (the HTTP timeouts bound its parts)
# Explicit client timeouts; lightkube's default is a flat 10 s. NOTE (finding 6): lightkube 1.0.2 builds
# in-cluster config with `User(token_file=...)`, which selects `BearerTokenFileAuth`: it reads the
# service-account token file on first use and re-reads it after a 401, so the kubelet's token rotation
# is picked up without re-creating the client. Verified in lightkube/config/client_adapter.py.
HTTP_TIMEOUT = httpx2.Timeout(connect=5, read=30, write=30, pool=5)
_REDACT_ASSIGNMENT = re.compile(r"(?i)((?:token|key|secret|password)=)\S+")
REDACTED = "***"
STATE_BY_PHASE = {
    "Pending": "starting",
    "Ready": "running",
    "Failed": "failed",
    "Terminating": "stopping",
    "Stopped": "stopped",
}


_CPU = re.compile(r"^(\d+(?:\.\d+)?)(m?)$")
_MEM = re.compile(r"^(\d+)(Ki|Mi|Gi|Ti|k|K|M|G|T)?$")
_MEM_UNITS = {None: 1, "k": 10**3, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
              "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


def _cpu_millis(value: str, var: str) -> float:
    mt = _CPU.match(value.strip())
    if not mt or float(mt.group(1)) <= 0:
        raise ValueError(f"{var}={value!r} is not a CPU quantity such as 1, 0.5 or 500m")
    return float(mt.group(1)) * (1 if mt.group(2) else 1000)


def _memory_bytes(value: str, var: str) -> int:
    mt = _MEM.match(value.strip())
    if not mt or int(mt.group(1)) <= 0:
        raise ValueError(f"{var}={value!r} is not a memory quantity such as 512Mi or 1Gi")
    return int(mt.group(1)) * _MEM_UNITS[mt.group(2)]


def pod_resources(cpu: str, memory: str) -> dict[str, dict[str, str]]:
    """Container resources for the MCP container. Limits come from the operator's configuration;
    requests are the usual small request, lowered to the limit when the limit is smaller."""
    cpu, memory = cpu.strip(), memory.strip()
    req_cpu = REQUEST_CPU if _cpu_millis(cpu, POD_CPU_ENV) >= _cpu_millis(REQUEST_CPU, "") else cpu
    req_mem = REQUEST_MEMORY if _memory_bytes(memory, POD_MEMORY_ENV) >= _memory_bytes(REQUEST_MEMORY, "") else memory
    return {"requests": {"cpu": req_cpu, "memory": req_mem}, "limits": {"cpu": cpu, "memory": memory}}


def resolve_tmp_size(tmp: str | None, memory: str) -> str:
    """Size of the memory-backed /tmp. tmpfs pages count against the container's memory limit, so an
    explicit size must leave at least half the limit for the process; the default shrinks to fit."""
    limit = _memory_bytes(memory, POD_MEMORY_ENV)
    if tmp is None:
        want = _memory_bytes(DEFAULT_POD_TMP, POD_TMP_ENV)
        if want * 2 <= limit:
            return DEFAULT_POD_TMP
        return f"{max(limit // 2 // 1024**2, 1)}Mi"
    tmp = tmp.strip()
    if _memory_bytes(tmp, POD_TMP_ENV) * 2 > limit:
        raise ValueError(f"{POD_TMP_ENV}={tmp!r} leaves no memory headroom under {POD_MEMORY_ENV}={memory!r}: "
                         "it must be at most half the memory limit")
    return tmp


def ports_from_env(value: str) -> tuple[int, ...]:
    out: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not 0 < int(part) < 65536:
            raise ValueError(f"{API_SERVER_PORTS_ENV} entry {part!r} is not a port number")
        out.append(int(part))
    return tuple(out)


def cidrs_from_env(value: str) -> tuple[str, ...]:
    out = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise ValueError(f"{API_SERVER_CIDRS_ENV} entry {part!r} is not a CIDR") from None
        out.append(part)
    return tuple(out)


def handle_for(tenant_id: str, server_id: str) -> RuntimeHandle:
    return RuntimeHandle(m.namespace_name(tenant_id), m.cr_name(server_id))


# --- documents (pure; unit tested) -----------------------------------------------------------


def namespace_doc(namespace: str, tenant_id: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {
                **MANAGED_BY,
                "pydanticos.io/mcp-tenant": hashlib.sha256(tenant_id.encode()).hexdigest()[:12],
                # Pods the operator creates here must run unprivileged: the namespace rejects the rest.
                "pod-security.kubernetes.io/enforce": "restricted",
            },
        },
    }


def role_binding_doc(namespace: str, name: str, role: str, sa: str, sa_namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": name, "namespace": namespace, "labels": MANAGED_BY},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
        "subjects": [{"kind": "ServiceAccount", "name": sa, "namespace": sa_namespace}],
    }


def default_deny_doc(namespace: str) -> dict[str, Any]:
    """Every pod in the namespace, including ones nothing else selects, starts with no traffic."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "default-deny", "namespace": namespace, "labels": MANAGED_BY},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
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


HostResolver = Callable[[str], Sequence[str]]
DEFAULT_NETWORK_PORTS = (443,)


def resolve_host_ips(host: str) -> list[str]:
    """The addresses a host name resolves to now (the default resolver; blocking)."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _public_cidrs(addresses: Sequence[str]) -> list[str]:
    """/32 and /128 CIDRs for the public addresses only: a name that resolves into private, loopback,
    link-local or reserved space (DNS rebinding, a poisoned or internal record) never opens egress."""
    private = [ipaddress.ip_network(r) for r in PRIVATE_RANGES]
    out: list[str] = []
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            continue
        if ip.version == 4 and any(ip in net for net in private):
            continue
        cidr = f"{ip}/{32 if ip.version == 4 else 128}"
        if cidr not in out:
            out.append(cidr)
    return out


def resolve_network(
    network: Mapping[str, Any] | None, resolve: HostResolver = resolve_host_ips
) -> dict[str, Any] | None:
    """Turn a catalog `{allow_host, allow_port, insecure_allow_all}` block into the egress it allows:
    `{"all": bool, "cidrs": [..], "ports": [..]}`; None when there is no block (today's default).

    The DNS answers are a SNAPSHOT taken here, at install/start: a host whose addresses change later is
    unreachable until the server is started again, and a CDN-backed host may hand out different
    addresses to the pod than to the platform. A leading dot (`.github.com`, a ToolHive subdomain
    wildcard) resolves the bare domain only; NetworkPolicy cannot match names, so subdomains with other
    addresses are not covered. A host that does not resolve is skipped (fail closed). With
    `insecure_allow_all` the hosts are not resolved at all. No hosts and no `insecure_allow_all` means
    no outbound network."""
    if network is None:
        return None
    ports = [int(p) for p in network.get("allow_port") or ()]
    if network.get("insecure_allow_all"):
        return {"all": True, "cidrs": [], "ports": ports}
    cidrs: list[str] = []
    for host in network.get("allow_host") or ():
        name = str(host).lstrip(".").strip()
        if not name:
            continue
        try:
            found = resolve(name)
        except Exception as exc:  # noqa: BLE001 - any resolver failure skips the host
            log.warning("network permission host %s did not resolve: %s", name, exc)
            continue
        cidrs.extend(c for c in _public_cidrs(found) if c not in cidrs)
    return {"all": False, "cidrs": cidrs, "ports": ports or list(DEFAULT_NETWORK_PORTS)}


def network_policy_doc(
    namespace: str,
    cr: str,
    *,
    platform_namespace: str,
    caller_labels: Sequence[dict[str, str]],
    egress_network: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """`egress_network` is `resolve_network`'s result: None keeps the public-internet rule (private ranges
    excluded, any port); otherwise exactly the allowed CIDRs/ports, or nothing beyond DNS."""
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
    ]
    if egress_network is None:
        egress.append({"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(PRIVATE_RANGES)}}]})
    else:
        ports = [{"protocol": "TCP", "port": p} for p in egress_network["ports"]]
        if egress_network["all"]:
            rule: dict[str, Any] = {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(PRIVATE_RANGES)}}]}
            if ports:
                rule["ports"] = ports
            egress.append(rule)
        elif egress_network["cidrs"]:
            egress.append({"to": [{"ipBlock": {"cidr": c}} for c in egress_network["cidrs"]], "ports": ports})
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
                            "podSelector": {"matchLabels": dict(labels)},
                        }
                        for labels in caller_labels
                    ],
                    "ports": [{"protocol": "TCP", "port": m.PROXY_PORT}],
                },
            ],
            "egress": egress,
        },
    }


PROXY_POD_LABELS = {"app": "mcpserver"}  # the operator's proxy runner pod; the backend is `app=<cr>`


def api_server_policy_doc(
    namespace: str, cr: str, api_server_cidrs: Sequence[str], ports: Sequence[int] = DEFAULT_API_SERVER_PORTS
) -> dict[str, Any]:
    """The proxy runner attaches to the backend through the API server; the backend never does."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": f"{cr}-proxy-api-server", "namespace": namespace, "labels": {**MANAGED_BY, m.POD_LABEL: cr}},
        "spec": {
            "podSelector": {"matchLabels": {m.POD_LABEL: cr, **PROXY_POD_LABELS}},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [{"ipBlock": {"cidr": c}} for c in api_server_cidrs],
                    "ports": [{"protocol": "TCP", "port": p} for p in ports],
                }
            ],
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
    cpu: str = DEFAULT_POD_CPU,
    memory: str = DEFAULT_POD_MEMORY,
    tmp_size: str = DEFAULT_POD_TMP,
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
                # No Kubernetes API credentials inside the pod, and no host namespaces.
                "automountServiceAccountToken": False,
                "hostNetwork": False,
                "hostPID": False,
                "hostIPC": False,
                # A pids limit (256 was planned) cannot be set per pod: Kubernetes only has the
                # kubelet-wide `podPidsLimit`, and the PodSpec has no field for it. The node's
                # setting and the pod count in `mcp-quota` bound it instead.
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
                        "resources": pod_resources(cpu, memory),
                        "volumeMounts": [{"name": "work", "mountPath": "/tmp"}],
                    }
                ],
                "volumes": [{"name": "work", "emptyDir": {"medium": "Memory", "sizeLimit": tmp_size}}],
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

    async def pod_list(self, namespace: str, label_selector: str) -> list[dict[str, Any]]:
        """The pods (as dicts, with `status`) matching the selector."""


class LightkubeApi:
    """`KubeApi` on lightkube's async client."""

    def __init__(
        self, client: Any | None = None, *, config: Any | None = None, call_timeout: float = CALL_TIMEOUT
    ) -> None:
        from lightkube import AsyncClient

        self._client = client or AsyncClient(config=config, field_manager=FIELD_MANAGER, timeout=HTTP_TIMEOUT)
        self._mcpserver = m.lightkube_mcpserver()
        self._call_timeout = call_timeout

    def _cls(self, kind: str) -> Any:
        from lightkube.resources.core_v1 import LimitRange, Namespace, Pod, ResourceQuota, Secret
        from lightkube.resources.networking_v1 import NetworkPolicy
        from lightkube.resources.rbac_authorization_v1 import RoleBinding

        return {
            "Namespace": Namespace,
            "Secret": Secret,
            "ResourceQuota": ResourceQuota,
            "LimitRange": LimitRange,
            "NetworkPolicy": NetworkPolicy,
            "RoleBinding": RoleBinding,
            "Pod": Pod,
            m.KIND: self._mcpserver,
        }[kind]

    async def _bounded(self, coro: Any) -> Any:
        return await asyncio.wait_for(coro, self._call_timeout)

    async def apply(self, doc: dict[str, Any]) -> None:
        cls = self._cls(doc["kind"])
        obj = cls.from_dict(doc) if hasattr(cls, "from_dict") else cls(**doc)
        await self._bounded(self._client.apply(obj, field_manager=FIELD_MANAGER, force=True))

    async def get(self, kind: str, namespace: str | None, name: str) -> dict[str, Any] | None:
        from lightkube import ApiError

        try:
            obj = await self._bounded(self._client.get(self._cls(kind), name, namespace=namespace))
        except ApiError as e:
            if e.status.code == 404:
                return None
            raise
        return _to_dict(obj)

    async def delete(self, kind: str, namespace: str | None, name: str) -> None:
        from lightkube import ApiError

        try:
            await self._bounded(self._client.delete(self._cls(kind), name, namespace=namespace))
        except ApiError as e:
            if e.status.code != 404:
                raise

    async def merge_patch(self, kind: str, namespace: str, name: str, body: dict[str, Any]) -> None:
        from lightkube import types

        await self._bounded(
            self._client.patch(self._cls(kind), name, body, namespace=namespace, patch_type=types.PatchType.MERGE)
        )

    async def _pods(self, namespace: str, label_selector: str) -> list[Any]:
        labels = dict(p.split("=", 1) for p in label_selector.split(","))
        return [pod async for pod in self._client.list(self._cls("Pod"), namespace=namespace, labels=labels)]

    async def pod_names(self, namespace: str, label_selector: str) -> list[str]:
        return [p.metadata.name for p in await self._bounded(self._pods(namespace, label_selector))]

    async def pod_list(self, namespace: str, label_selector: str) -> list[dict[str, Any]]:
        return [_to_dict(p) for p in await self._bounded(self._pods(namespace, label_selector))]

    async def pod_log(self, namespace: str, pod: str, tail: int) -> str:
        async def read() -> str:
            return "\n".join(
                [
                    line
                    async for line in self._client.log(
                        pod, namespace=namespace, container=m.MCP_CONTAINER, tail_lines=tail
                    )
                ]
            )

        return await self._bounded(read())


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
        ingress_labels: Sequence[dict[str, str]] | None = None,
        worker_service_account: str | None = None,
        api_service_account: str | None = None,
        pod_tmp_size: str | None = None,
        api_server_cidrs: Sequence[str] | None = None,
        api_server_ports: Sequence[int] | None = None,
        pod_cpu: str | None = None,
        pod_memory: str | None = None,
        resolve_host: HostResolver | None = None,
    ) -> None:
        self._api = api or LightkubeApi()
        self._resolve_host = resolve_host or resolve_host_ips  # injectable for tests
        self._platform_namespace = platform_namespace
        # Platform pods that may call the proxy port: the worker and the API, by label.
        self._ingress_labels = tuple(ingress_labels) if ingress_labels else DEFAULT_CALLER_LABELS
        self._worker_sa = worker_service_account or os.environ.get(WORKER_SA_ENV) or "pydanticos-mcp-worker"
        self._api_sa = api_service_account or os.environ.get(API_SA_ENV) or "pydanticos-mcp-api"
        self._secret_values: dict[RuntimeHandle, tuple[str, ...]] = {}  # for redacting logs; never logged
        self._poll_interval = 1.0
        self._delete_timeout = 120.0
        # The proxy runner reaches the backend through the Kubernetes API server, whose address is
        # cluster specific: `kubectl get endpoints kubernetes -n default` (and the `kubernetes`
        # Service ClusterIP), as comma separated CIDRs, e.g. "10.43.0.1/32,192.168.5.3/32".
        self._api_server_cidrs = (
            tuple(api_server_cidrs)
            if api_server_cidrs is not None
            else cidrs_from_env(os.environ.get(API_SERVER_CIDRS_ENV, ""))
        )
        self._api_server_ports = (
            tuple(api_server_ports)
            if api_server_ports is not None
            else ports_from_env(os.environ.get(API_SERVER_PORTS_ENV, "")) or DEFAULT_API_SERVER_PORTS
        )
        self._cpu = (pod_cpu or os.environ.get(POD_CPU_ENV) or DEFAULT_POD_CPU).strip()
        self._memory = (pod_memory or os.environ.get(POD_MEMORY_ENV) or DEFAULT_POD_MEMORY).strip()
        pod_resources(self._cpu, self._memory)  # validate now rather than at the first start
        self._tmp = resolve_tmp_size(pod_tmp_size or os.environ.get(POD_TMP_ENV) or None, self._memory)

    async def start(self, spec: RuntimeSpec) -> RuntimeHandle:
        h = RuntimeHandle(m.namespace_name(spec.tenant_id), m.cr_name(spec.server_id))
        await self._api.apply(namespace_doc(h.namespace, spec.tenant_id))
        # The per-tenant grants first: the platform accounts' cluster-wide role cannot touch anything inside the
        # namespace, these bindings are what let them work in this one.
        await self._api.apply(
            role_binding_doc(h.namespace, f"{TENANT_ROLE}-worker", TENANT_ROLE, self._worker_sa, self._platform_namespace)
        )
        # The API drives start/stop/restart/delete itself, so it gets the same write role. A new binding
        # name: a RoleBinding's roleRef is immutable, so the earlier read-only `-api` one cannot be edited.
        await self._api.apply(
            role_binding_doc(h.namespace, f"{TENANT_ROLE}-api-write", TENANT_ROLE, self._api_sa, self._platform_namespace)
        )
        await self._api.apply(default_deny_doc(h.namespace))
        await self._api.apply(limit_range_doc(h.namespace))
        await self._api.apply(quota_doc(h.namespace))
        if spec.secret_refs:
            await self._api.apply(secret_doc(h.namespace, h.cr_name, dict(spec.secret_refs)))
        else:  # a previous run's values must not outlive a spec that no longer has secrets
            await self._api.delete("Secret", h.namespace, m.secret_name(h.cr_name))
        self._secret_values[h] = tuple(v for v in spec.secret_refs.values() if v)
        # Resolved off the event loop: getaddrinfo blocks. Re-resolved at every start (a snapshot per start).
        egress_network = await asyncio.to_thread(resolve_network, spec.network, self._resolve_host)
        await self._api.apply(
            network_policy_doc(
                h.namespace,
                h.cr_name,
                platform_namespace=self._platform_namespace,
                caller_labels=self._ingress_labels,
                egress_network=egress_network,
            )
        )
        if self._api_server_cidrs:
            await self._api.apply(api_server_policy_doc(h.namespace, h.cr_name, self._api_server_cidrs, self._api_server_ports)
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
                cpu=self._cpu,
                memory=self._memory,
                tmp_size=self._tmp,
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
        # The pods keep running until the operator has torn them down; removing their NetworkPolicy
        # first would leave them unisolated. On timeout the policies stay and delete() can be re-run.
        await self._wait_gone(handle)
        await self._api.delete("Secret", handle.namespace, m.secret_name(handle.cr_name))
        await self._api.delete("NetworkPolicy", handle.namespace, f"{handle.cr_name}-proxy-api-server")
        await self._api.delete("NetworkPolicy", handle.namespace, f"{handle.cr_name}-isolation")

    async def _wait_gone(self, handle: RuntimeHandle) -> None:
        deadline = time.monotonic() + self._delete_timeout
        selector = f"{m.POD_LABEL}={handle.cr_name}"
        while True:
            if await self._api.get(m.KIND, handle.namespace, handle.cr_name) is None and not await self._api.pod_names(
                handle.namespace, selector
            ):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{handle.namespace}/{handle.cr_name} still has pods; its NetworkPolicy was kept")
            await asyncio.sleep(self._poll_interval)

    async def status(self, handle: RuntimeHandle) -> RuntimeStatus:
        endpoint = self.endpoint(handle)
        cr = await self._api.get(m.KIND, handle.namespace, handle.cr_name)
        if cr is None:
            return RuntimeStatus("absent", endpoint, "not found")
        st = cr.get("status") or {}
        message = st.get("message", "")
        generation = (cr.get("metadata") or {}).get("generation")
        if generation is not None and st.get("observedGeneration") != generation:
            # The operator has not seen the latest spec yet: its phase describes the previous one.
            return RuntimeStatus("starting", endpoint, message or "waiting for the operator")
        phase = st.get("phase", "")
        state = STATE_BY_PHASE.get(phase, "starting")
        if state == "starting":
            reason = await self._pod_failure(handle)
            if reason:
                return RuntimeStatus("failed", endpoint, reason)
        return RuntimeStatus(state, endpoint, message)  # type: ignore[arg-type]

    async def _pod_failure(self, handle: RuntimeHandle) -> str | None:
        """While the operator still says Pending, a crash-looping or unpullable pod is a failure."""
        try:
            pods = await self._api.pod_list(handle.namespace, f"{m.POD_LABEL}={handle.cr_name}")
        except Exception:  # noqa: BLE001 - the extra detail is best effort; the phase stays authoritative
            return None
        for pod in pods:
            pst = pod.get("status") or {}
            for cs in (*(pst.get("initContainerStatuses") or []), *(pst.get("containerStatuses") or [])):
                waiting = (cs.get("state") or {}).get("waiting") or {}
                terminated = (cs.get("lastState") or {}).get("terminated") or {}
                for reason, detail in ((waiting.get("reason"), waiting.get("message")),
                                       (terminated.get("reason"), terminated.get("message"))):
                    if reason in FAILED_REASONS:
                        name = (pod.get("metadata") or {}).get("name", "pod")
                        return f"{reason} ({name}/{cs.get('name', '?')})" + (f": {detail}" if detail else "")
        return None

    async def logs(self, handle: RuntimeHandle, tail: int = 200, secret_values: Sequence[str] = ()) -> list[str]:
        """Backend pod log lines with secret values and `token|key|secret|password=...` redacted.
        The values come from this process's own `start()` and from `secret_values` (a different process,
        such as the API, passes the spec's values); the pattern covers the rest."""
        pods = await self._api.pod_names(handle.namespace, f"{m.POD_LABEL}={handle.cr_name}")
        backend = f"{handle.cr_name}-0"
        if backend not in pods:
            return []
        text = await self._api.pod_log(handle.namespace, backend, tail)
        values = sorted({*self._secret_values.get(handle, ()), *(v for v in secret_values if v)}, key=len, reverse=True)
        for v in values:
            text = text.replace(v, REDACTED)
        text = _REDACT_ASSIGNMENT.sub(lambda mt: mt.group(1) + REDACTED, text)
        return text.splitlines()

    def endpoint(self, handle: RuntimeHandle) -> str:
        return m.endpoint_url(handle.namespace, handle.cr_name)

    async def wait_for(self, handle: RuntimeHandle, state: str, timeout: float = 180.0) -> RuntimeStatus:
        """Poll until `state`, or a failed state (its message holds the reason), or `timeout` seconds in
        total: `timeout` is an overall deadline that also bounds a status call that hangs."""
        deadline = time.monotonic() + timeout
        last = RuntimeStatus("starting", self.endpoint(handle), "no status yet")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last
            try:
                last = await asyncio.wait_for(self.status(handle), remaining)
            except TimeoutError:
                return RuntimeStatus(last.state, last.endpoint, "status check timed out")
            if last.state == state or (last.state == "failed" and state != "failed"):
                return last
            await asyncio.sleep(min(2, max(deadline - time.monotonic(), 0)))
