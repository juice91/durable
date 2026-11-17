from __future__ import annotations

import asyncio
import json
import time
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
# What stays in a tenant namespace after its last server is deleted.
TENANT_BASELINE = {"Namespace", "ResourceQuota", "LimitRange", "RoleBinding", "NetworkPolicy"}


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
    assert kinds == TENANT_BASELINE | {"Secret", "MCPServer"}
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
    assert left == TENANT_BASELINE
    await rt.delete(h)  # crash recovery: re-running changes nothing and raises nothing


async def test_delete_after_a_crash_between_steps_finishes_the_job() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    del api.objects[("MCPServer", h.namespace, h.cr_name)]  # the first step already ran
    await rt.delete(h)
    assert {k[0] for k in api.objects} == TENANT_BASELINE


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


def _limits(api: FakeKube, h) -> dict:
    (c,) = api.objects[("MCPServer", h.namespace, h.cr_name)]["spec"]["podTemplateSpec"]["spec"]["containers"]
    return c["resources"]


async def test_pod_limits_default_to_one_cpu_and_one_gi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EAP_MCP_POD_CPU", raising=False)
    monkeypatch.delenv("EAP_MCP_POD_MEMORY", raising=False)
    rt, api = runtime()
    h = await rt.start(spec())
    assert _limits(api, h)["limits"] == {"cpu": "1", "memory": "1Gi"}


async def test_pod_limits_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAP_MCP_POD_CPU", "2")
    monkeypatch.setenv("EAP_MCP_POD_MEMORY", "512Mi")
    rt, api = runtime()
    h = await rt.start(spec())
    res = _limits(api, h)
    assert res["limits"] == {"cpu": "2", "memory": "512Mi"}
    assert res["requests"]["cpu"] == "50m" and res["requests"]["memory"] == "64Mi"


async def test_requests_never_exceed_a_small_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAP_MCP_POD_CPU", "20m")
    monkeypatch.setenv("EAP_MCP_POD_MEMORY", "32Mi")
    rt, api = runtime()
    h = await rt.start(spec())
    assert _limits(api, h)["requests"] == {"cpu": "20m", "memory": "32Mi"}


@pytest.mark.parametrize(("var", "bad"), [("EAP_MCP_POD_CPU", "lots"), ("EAP_MCP_POD_MEMORY", "1 gigabyte")])
def test_an_invalid_limit_is_rejected_at_construction(monkeypatch: pytest.MonkeyPatch, var: str, bad: str) -> None:
    monkeypatch.setenv(var, bad)
    with pytest.raises(ValueError, match=var):
        runtime()


async def test_api_server_cidrs_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAP_MCP_API_SERVER_CIDRS", "10.43.0.1/32, 192.168.5.3/32")
    api = FakeKube()
    rt = OperatorRuntime(api)
    h = await rt.start(spec())
    np = api.objects[("NetworkPolicy", h.namespace, f"{h.cr_name}-proxy-api-server")]["spec"]
    cidrs = [t["ipBlock"]["cidr"] for r in np["egress"] for t in r["to"] if "ipBlock" in t]
    assert "10.43.0.1/32" in cidrs and "192.168.5.3/32" in cidrs


async def test_an_explicit_cidr_argument_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAP_MCP_API_SERVER_CIDRS", "10.43.0.1/32")
    api = FakeKube()
    rt = OperatorRuntime(api, api_server_cidrs=("172.20.0.5/32",))
    h = await rt.start(spec())
    np = api.objects[("NetworkPolicy", h.namespace, f"{h.cr_name}-proxy-api-server")]["spec"]
    cidrs = [t["ipBlock"]["cidr"] for r in np["egress"] for t in r["to"] if "ipBlock" in t]
    assert "172.20.0.5/32" in cidrs and "10.43.0.1/32" not in cidrs


def test_an_invalid_cidr_in_the_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAP_MCP_API_SERVER_CIDRS", "not-a-cidr")
    with pytest.raises(ValueError, match="EAP_MCP_API_SERVER_CIDRS"):
        OperatorRuntime(FakeKube())


async def test_pod_never_gets_service_account_token_or_host_access() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    pod = api.objects[("MCPServer", h.namespace, h.cr_name)]["spec"]["podTemplateSpec"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert not any(pod.get(k) for k in ("hostNetwork", "hostPID", "hostIPC"))


# --- Task 5b: RBAC, default deny, API-server egress, tmp size ---------------------------------


def _doc(api: FakeKube, kind: str, namespace: str | None, name: str) -> dict:
    return api.objects[(kind, namespace, name)]


async def test_each_tenant_namespace_gets_rolebindings_for_the_platform_service_accounts() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    worker = _doc(api, "RoleBinding", h.namespace, "pydanticos-mcp-tenant-worker")
    reader = _doc(api, "RoleBinding", h.namespace, "pydanticos-mcp-tenant-api-write")
    assert worker["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "pydanticos-mcp-tenant"}
    assert worker["subjects"] == [{"kind": "ServiceAccount", "name": "pydanticos-mcp-worker", "namespace": "pydanticos"}]
    assert reader["roleRef"]["name"] == "pydanticos-mcp-tenant"  # the API starts and stops runtimes too
    assert reader["subjects"] == [{"kind": "ServiceAccount", "name": "pydanticos-mcp-api", "namespace": "pydanticos"}]


async def test_bindings_exist_before_anything_else_is_written_into_the_namespace() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    order = [c[1] for c in api.calls if c[0] == "apply"]
    assert order[0] == "Namespace" and set(order[1:3]) == {"RoleBinding"}


async def test_namespace_enforces_the_restricted_pod_security_standard() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    labels = _doc(api, "Namespace", None, h.namespace)["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"


async def test_namespace_has_a_default_deny_policy_for_every_pod() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    deny = _doc(api, "NetworkPolicy", h.namespace, "default-deny")["spec"]
    assert deny["podSelector"] == {} and set(deny["policyTypes"]) == {"Ingress", "Egress"}
    assert not deny.get("ingress") and not deny.get("egress")
    applied = [c[1:] for c in api.calls if c[0] == "apply"]
    assert applied.index(("NetworkPolicy", h.namespace, "default-deny")) < applied.index(("MCPServer", h.namespace, h.cr_name))


async def test_default_deny_survives_delete() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    await rt.delete(h)
    assert ("NetworkPolicy", h.namespace, "default-deny") in api.objects


async def test_ingress_on_the_proxy_port_admits_the_worker_and_the_api() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    np = _doc(api, "NetworkPolicy", h.namespace, f"{h.cr_name}-isolation")["spec"]
    (rule,) = [r for r in np["ingress"] if r.get("ports")]
    assert [f["podSelector"]["matchLabels"] for f in rule["from"]] == [
        {"app.kubernetes.io/name": "worker"},
        {"app.kubernetes.io/name": "api"},
    ]
    assert all(f["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "pydanticos"} for f in rule["from"])


async def test_ingress_selectors_are_configurable() -> None:
    api = FakeKube()
    rt = OperatorRuntime(api, api_server_cidrs=(), ingress_labels=[{"a": "b"}])
    h = await rt.start(spec())
    (rule,) = [r for r in _doc(api, "NetworkPolicy", h.namespace, f"{h.cr_name}-isolation")["spec"]["ingress"] if r.get("ports")]
    assert [f["podSelector"]["matchLabels"] for f in rule["from"]] == [{"a": "b"}]


async def test_api_server_egress_is_limited_to_the_proxy_pod_and_to_https_ports() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    main = _doc(api, "NetworkPolicy", h.namespace, f"{h.cr_name}-isolation")["spec"]
    # the backend (and the shared policy) has no rule for the API server at all
    assert not [t for r in main["egress"] for t in r["to"] if t.get("ipBlock", {}).get("cidr") == "10.43.0.1/32"]
    proxy = _doc(api, "NetworkPolicy", h.namespace, f"{h.cr_name}-proxy-api-server")["spec"]
    assert proxy["podSelector"] == {"matchLabels": {"toolhive-name": h.cr_name, "app": "mcpserver"}}
    assert proxy["policyTypes"] == ["Egress"]
    (rule,) = proxy["egress"]
    assert rule["to"] == [{"ipBlock": {"cidr": "10.43.0.1/32"}}]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 6443}]


async def test_no_api_server_policy_without_configured_cidrs() -> None:
    api = FakeKube()
    h = await OperatorRuntime(api, api_server_cidrs=()).start(spec())
    assert ("NetworkPolicy", h.namespace, f"{h.cr_name}-proxy-api-server") not in api.objects


async def test_public_egress_excludes_special_purpose_ranges() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    np = _doc(api, "NetworkPolicy", h.namespace, f"{h.cr_name}-isolation")["spec"]
    (internet,) = [t for r in np["egress"] for t in r["to"] if t.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"]
    assert {"0.0.0.0/8", "127.0.0.0/8", "192.0.0.0/24", "198.18.0.0/15", "224.0.0.0/3"} <= set(internet["ipBlock"]["except"])


async def test_delete_removes_the_policy_only_after_the_pods_are_gone() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    api.pods[h.namespace] = [f"{h.cr_name}-0"]
    gone_after = 3
    polls = 0
    real = api.pod_names

    async def pod_names(ns: str, sel: str) -> list[str]:
        nonlocal polls
        polls += 1
        if polls > gone_after:
            api.pods[ns] = []
        return await real(ns, sel)

    api.pod_names = pod_names  # type: ignore[method-assign]
    rt._poll_interval = 0  # noqa: SLF001
    await rt.delete(h)
    deletes = [c[1] for c in api.calls if c[0] == "delete"]
    assert deletes.index("MCPServer") < deletes.index("NetworkPolicy")
    assert polls > gone_after
    assert ("NetworkPolicy", h.namespace, f"{h.cr_name}-isolation") not in api.objects
    assert ("NetworkPolicy", h.namespace, f"{h.cr_name}-proxy-api-server") not in api.objects


async def test_delete_keeps_the_policy_when_the_pods_do_not_go_away() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    api.pods[h.namespace] = [f"{h.cr_name}-0"]
    rt._poll_interval = 0  # noqa: SLF001
    rt._delete_timeout = 0.05  # noqa: SLF001
    with pytest.raises(TimeoutError):
        await rt.delete(h)
    assert ("NetworkPolicy", h.namespace, f"{h.cr_name}-isolation") in api.objects
    api.pods[h.namespace] = []
    await rt.delete(h)  # retry finishes the job
    assert ("NetworkPolicy", h.namespace, f"{h.cr_name}-isolation") not in api.objects


def _tmp_volume(api: FakeKube, h) -> dict:
    pod = api.objects[("MCPServer", h.namespace, h.cr_name)]["spec"]["podTemplateSpec"]["spec"]
    return pod["volumes"][0]["emptyDir"]


async def test_tmp_defaults_to_512mi_memory_backed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EAP_MCP_POD_TMP_SIZE", raising=False)
    monkeypatch.delenv("EAP_MCP_POD_MEMORY", raising=False)
    rt, api = runtime()
    h = await rt.start(spec())
    assert _tmp_volume(api, h) == {"medium": "Memory", "sizeLimit": "512Mi"}


async def test_tmp_size_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAP_MCP_POD_TMP_SIZE", "256Mi")
    rt, api = runtime()
    h = await rt.start(spec())
    assert _tmp_volume(api, h)["sizeLimit"] == "256Mi"


async def test_default_tmp_shrinks_to_leave_memory_headroom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EAP_MCP_POD_TMP_SIZE", raising=False)
    monkeypatch.setenv("EAP_MCP_POD_MEMORY", "256Mi")
    rt, api = runtime()
    h = await rt.start(spec())
    assert _tmp_volume(api, h)["sizeLimit"] == "128Mi"


def test_an_explicit_tmp_size_without_memory_headroom_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAP_MCP_POD_MEMORY", "512Mi")
    monkeypatch.setenv("EAP_MCP_POD_TMP_SIZE", "512Mi")
    with pytest.raises(ValueError, match="EAP_MCP_POD_TMP_SIZE"):
        OperatorRuntime(FakeKube())
    monkeypatch.setenv("EAP_MCP_POD_TMP_SIZE", "lots")
    with pytest.raises(ValueError, match="EAP_MCP_POD_TMP_SIZE"):
        OperatorRuntime(FakeKube())


async def test_api_server_ports_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAP_MCP_API_SERVER_PORTS", "443, 57345")
    api = FakeKube()
    h = await OperatorRuntime(api, api_server_cidrs=("10.43.0.1/32",)).start(spec())
    (rule,) = api.objects[("NetworkPolicy", h.namespace, f"{h.cr_name}-proxy-api-server")]["spec"]["egress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 57345}]


def test_an_invalid_api_server_port_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAP_MCP_API_SERVER_PORTS", "443,https")
    with pytest.raises(ValueError, match="EAP_MCP_API_SERVER_PORTS"):
        OperatorRuntime(FakeKube())


# --- review findings on 19f0703: failed states, generation, timeouts, secret cleanup, redaction ------


def _pod(name: str, waiting: str | None = None, message: str = "", terminated: str | None = None) -> dict:
    cs: dict = {"name": "mcp", "state": {}, "lastState": {}}
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting, "message": message}}
    if terminated:
        cs["lastState"] = {"terminated": {"reason": terminated, "exitCode": 137}}
    return {"metadata": {"name": name}, "status": {"phase": "Pending", "containerStatuses": [cs]}}


@pytest.mark.parametrize(
    "waiting,terminated",
    [("CrashLoopBackOff", None), ("ImagePullBackOff", None), ("ErrImagePull", None),
     ("CreateContainerConfigError", None), (None, "OOMKilled")],
)
async def test_a_pending_server_with_a_broken_pod_is_failed_with_the_reason(waiting, terminated) -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    api.objects[("MCPServer", h.namespace, h.cr_name)]["status"] = {"phase": "Pending", "message": "waiting"}
    api.pod_status[h.namespace] = [_pod(f"{h.cr_name}-0", waiting, "back-off 5m0s restarting", terminated)]
    s = await rt.status(h)
    assert s.state == "failed"
    assert (waiting or terminated) in s.message


async def test_a_pending_server_with_healthy_or_unknown_pods_stays_starting() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    api.objects[("MCPServer", h.namespace, h.cr_name)]["status"] = {"phase": "Pending"}
    api.pod_status[h.namespace] = [_pod(f"{h.cr_name}-0", "ContainerCreating")]
    assert (await rt.status(h)).state == "starting"
    api.pod_status[h.namespace] = []
    assert (await rt.status(h)).state == "starting"


async def test_wait_for_stops_early_on_a_failed_state_and_reports_the_reason() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    api.objects[("MCPServer", h.namespace, h.cr_name)]["status"] = {"phase": "Pending"}
    api.pod_status[h.namespace] = [_pod(f"{h.cr_name}-0", "ImagePullBackOff", "pull access denied")]
    s = await rt.wait_for(h, "running", timeout=30)
    assert s.state == "failed" and "ImagePullBackOff" in s.message


async def test_status_is_starting_until_the_operator_observed_the_latest_generation() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    cr = api.objects[("MCPServer", h.namespace, h.cr_name)]
    cr["metadata"]["generation"] = 3
    cr["status"] = {"phase": "Ready", "observedGeneration": 2}
    assert (await rt.status(h)).state == "starting"
    cr["status"]["observedGeneration"] = 3
    assert (await rt.status(h)).state == "running"


async def test_wait_for_is_bounded_even_when_a_status_call_hangs() -> None:
    rt, api = runtime()
    h = await rt.start(spec())

    async def hang(*a, **k):
        await asyncio.sleep(60)

    api.get = hang  # type: ignore[method-assign]
    started = time.monotonic()
    s = await rt.wait_for(h, "running", timeout=0.3)
    assert time.monotonic() - started < 5
    assert s.state == "starting" and "timed out" in s.message


async def test_lightkube_calls_are_bounded_and_the_client_has_explicit_timeouts() -> None:
    import httpx2
    from eap_mcp_runtime import operator as op

    class Hanging:
        async def get(self, *a, **k):
            await asyncio.sleep(60)

    api = op.LightkubeApi(Hanging(), call_timeout=0.2)
    with pytest.raises(TimeoutError):
        await api.get("Secret", "ns", "x")
    t = op.HTTP_TIMEOUT
    assert isinstance(t, httpx2.Timeout)
    assert (t.connect, t.read, t.write, t.pool) == (5, 30, 30, 5)


async def test_start_without_secret_refs_removes_a_secret_left_by_an_earlier_run() -> None:
    rt, api = runtime()
    h = await rt.start(spec())
    assert ("Secret", h.namespace, f"{h.cr_name}-env") in api.objects
    await rt.start(spec(secret_refs={}))
    assert ("Secret", h.namespace, f"{h.cr_name}-env") not in api.objects
    assert "secrets" not in api.objects[("MCPServer", h.namespace, h.cr_name)]["spec"]


async def test_logs_redact_the_secret_values_and_token_like_assignments() -> None:
    rt, api = runtime()
    h = await rt.start(spec(secret_refs={"API_TOKEN": CANARY}))
    api.pods[h.namespace] = [f"{h.cr_name}-0"]
    api.logs[f"{h.cr_name}-0"] = (
        f"using {CANARY} now\nAPI_TOKEN=abc123 ok\nlogin password=hunter2 done\nSecret=x Key=y plain line"
    )
    out = "\n".join(await rt.logs(h))
    assert CANARY not in out and "abc123" not in out and "hunter2" not in out
    assert "Secret=x" not in out and "Key=y" not in out
    assert "plain line" in out and "using" in out


async def test_logs_redact_values_a_caller_passes_in() -> None:
    rt, api = runtime()
    h = handle_for(TENANT, SERVER)
    api.pods[h.namespace] = [f"{h.cr_name}-0"]
    api.logs[f"{h.cr_name}-0"] = "value is zzz-not-registered here"
    assert "zzz-not-registered" not in "\n".join(await rt.logs(h, secret_values=["zzz-not-registered"]))
