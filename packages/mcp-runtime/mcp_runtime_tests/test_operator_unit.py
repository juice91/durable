from __future__ import annotations

import json
import uuid

import pytest
from eap_mcp_runtime import OperatorRuntime, RuntimeSpec, handle_for
from eap_mcp_runtime import k8s_models as m
from eap_mcp_runtime.operator import PRIVATE_RANGES

from mcp_runtime_tests.fakes import FakeKube

pytestmark = pytest.mark.anyio

TENANT = "tenant-a"
SERVER = str(uuid.UUID("0123456789ab4cde8f01234567890abc"))
CANARY = "s3cr3t-canary-value-9f2"


def spec(**kw) -> RuntimeSpec:
    base = dict(
        tenant_id=TENANT,
        server_id=SERVER,
        name="Echo",
        image="mcp/everything",
        args=("--flag",),
        env={"LOG_LEVEL": "info"},
        secret_refs={"API_TOKEN": CANARY},
    )
    return RuntimeSpec(**{**base, **kw})


def runtime(api: FakeKube | None = None, **kw) -> tuple[OperatorRuntime, FakeKube]:
    api = api or FakeKube()
    return OperatorRuntime(api, api_server_cidrs=("10.43.0.1/32",), **kw), api


def test_names_are_deterministic_and_tenant_scoped() -> None:
    h = handle_for(TENANT, SERVER)
    assert h.namespace == m.namespace_name(TENANT)
    assert h.namespace.startswith("mcp-tenant-") and len(h.namespace) == len("mcp-tenant-") + 12
    assert h.cr_name == "s-0123456789ab"
    assert handle_for("tenant-b", SERVER).namespace != h.namespace


def test_endpoint_follows_the_operator_proxy_service() -> None:
    rt, _ = runtime()
    h = handle_for(TENANT, SERVER)
    assert rt.endpoint(h) == f"http://mcp-s-0123456789ab-proxy.{h.namespace}.svc:8080/mcp"


async def test_start_writes_cr_secret_policy_and_quota() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    kinds = {k[0] for k in api.objects}
    assert kinds == {"Namespace", "ResourceQuota", "LimitRange", "Secret", "NetworkPolicy", "MCPServer"}
    assert ("Namespace", None, h.namespace) in api.objects
    cr = api.objects[("MCPServer", h.namespace, h.cr_name)]
    assert cr["apiVersion"] == "toolhive.stacklok.dev/v1beta1"
    assert cr["spec"]["image"] == "mcp/everything"
    assert cr["spec"]["args"] == ["--flag"]
    assert cr["spec"]["transport"] == "stdio"
    assert cr["spec"]["replicas"] == 1 and cr["spec"]["backendReplicas"] == 1


async def test_secret_values_are_only_in_the_kubernetes_secret() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    cr = api.objects[("MCPServer", h.namespace, h.cr_name)]
    assert cr["spec"]["secrets"] == [
        {"name": m.secret_name(h.cr_name), "key": "API_TOKEN", "targetEnvName": "API_TOKEN"}
    ]
    assert cr["spec"]["env"] == [{"name": "LOG_LEVEL", "value": "info"}]
    for key, doc in api.objects.items():
        text = json.dumps(doc)
        if key[0] == "Secret":
            assert doc["stringData"] == {"API_TOKEN": CANARY}
        else:
            assert CANARY not in text, key
    assert CANARY not in repr(spec())


async def test_container_is_locked_down_and_limited() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    pod = api.objects[("MCPServer", h.namespace, h.cr_name)]["spec"]["podTemplateSpec"]["spec"]
    (c,) = pod["containers"]
    assert c["name"] == "mcp"
    sc = c["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["readOnlyRootFilesystem"] is True
    assert sc["allowPrivilegeEscalation"] is False and sc["capabilities"] == {"drop": ["ALL"]}
    assert c["resources"]["limits"]["memory"] and c["resources"]["limits"]["cpu"]
    assert pod["volumes"][0]["emptyDir"]["medium"] == "Memory"
    assert not any(v.get("hostPath") for v in pod["volumes"])


async def test_network_policy_isolates_the_pod() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    np = api.objects[("NetworkPolicy", h.namespace, f"{h.cr_name}-isolation")]["spec"]
    assert np["podSelector"] == {"matchLabels": {"toolhive-name": h.cr_name}}
    assert set(np["policyTypes"]) == {"Ingress", "Egress"}
    worker = [r for r in np["ingress"] if r.get("ports")]
    assert len(worker) == 1 and worker[0]["ports"] == [{"protocol": "TCP", "port": 8080}]
    assert worker[0]["from"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "pydanticos"
    }
    internet = [t for r in np["egress"] for t in r["to"] if t.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"]
    assert internet and set(internet[0]["ipBlock"]["except"]) == set(PRIVATE_RANGES)


async def test_quota_is_per_tenant_namespace() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    hard = api.objects[("ResourceQuota", h.namespace, "mcp-quota")]["spec"]["hard"]
    assert {"pods", "limits.cpu", "limits.memory"} <= set(hard)


async def test_no_secret_object_without_secret_refs() -> None:
    rt, api = runtime()
    await rt.start(spec(secret_refs={}))
    assert "Secret" not in {k[0] for k in api.objects}
    (cr,) = [d for k, d in api.objects.items() if k[0] == "MCPServer"]
    assert "secrets" not in cr["spec"]


async def test_http_transport_sets_the_mcp_port() -> None:
    rt, api = runtime()
    h = await rt.start(spec(transport="streamable-http", port=9000))
    assert api.objects[("MCPServer", h.namespace, h.cr_name)]["spec"]["mcpPort"] == 9000


async def test_start_twice_is_idempotent_and_restarts_a_stopped_server() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    await rt.stop(h)
    cr = api.objects[("MCPServer", h.namespace, h.cr_name)]
    assert cr["spec"]["replicas"] == 0 and cr["spec"]["backendReplicas"] == 0
    assert await rt.start(spec()) == h
    assert api.objects[("MCPServer", h.namespace, h.cr_name)]["spec"]["replicas"] == 1
    assert len([k for k in api.objects if k[0] == "MCPServer"]) == 1


async def test_stop_of_a_missing_server_is_a_noop() -> None:
    rt, _ = runtime()
    await rt.stop(handle_for(TENANT, SERVER))


async def test_status_maps_operator_phases() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    assert (await rt.status(h)).state == "starting"  # no status yet
    cr = api.objects[("MCPServer", h.namespace, h.cr_name)]
    for phase, state in [("Pending", "starting"), ("Ready", "running"), ("Failed", "failed"),
                         ("Stopped", "stopped"), ("Terminating", "stopping")]:
        cr["status"] = {"phase": phase, "message": phase.lower()}
        s = await rt.status(h)
        assert (s.state, s.message, s.endpoint) == (state, phase.lower(), rt.endpoint(h))
    await rt.delete(h)
    assert (await rt.status(h)).state == "absent"


async def test_delete_removes_cr_secret_and_policy_and_can_be_rerun() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    await rt.delete(h)
    left = {k[0] for k in api.objects}
    assert left == {"Namespace", "ResourceQuota", "LimitRange"}
    await rt.delete(h)  # crash recovery: re-running changes nothing and raises nothing


async def test_delete_after_a_crash_between_steps_finishes_the_job() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    del api.objects[("MCPServer", h.namespace, h.cr_name)]  # the first step already ran
    await rt.delete(h)
    assert {k[0] for k in api.objects} == {"Namespace", "ResourceQuota", "LimitRange"}


async def test_logs_tail_the_backend_pod_only() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    assert await rt.logs(h) == []
    api.pods[h.namespace] = [f"{h.cr_name}-abc", f"{h.cr_name}-0"]
    api.logs[f"{h.cr_name}-0"] = "one\ntwo\nthree"
    assert await rt.logs(h, tail=2) == ["two", "three"]


def test_unknown_phase_falls_back_to_starting() -> None:
    from eap_mcp_runtime.operator import STATE_BY_PHASE

    assert set(STATE_BY_PHASE) == set(m.PHASES)


def test_env_names_lists_plain_and_secret_names() -> None:
    assert spec().env_names == ("LOG_LEVEL", "API_TOKEN")
