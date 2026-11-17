"""deploy/k8s/rbac-mcp.yaml, networkpolicy.yaml and up.sh: the platform's least-privilege contract."""

from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
K8S = ROOT / "deploy" / "k8s"


def docs(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((K8S / name).read_text()) if d]


def by_kind(items: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in items if d["kind"] == kind}


def grants(role: dict) -> set[tuple[str, str, str]]:
    return {(g, r, v) for rule in role["rules"] for g in rule["apiGroups"] for r in rule["resources"] for v in rule["verbs"]}


def test_there_is_no_cluster_wide_binding_of_secret_or_pod_access() -> None:
    items = docs("rbac-mcp.yaml")
    for binding in by_kind(items, "ClusterRoleBinding").values():
        role = by_kind(items, "ClusterRole")[binding["roleRef"]["name"]]
        resources = {r for rule in role["rules"] for r in rule["resources"]}
        assert not resources & {"secrets", "pods", "pods/log", "networkpolicies", "mcpservers"}, binding["metadata"]["name"]


def test_cluster_wide_grant_is_only_namespaces_and_rolebindings() -> None:
    items = docs("rbac-mcp.yaml")
    (binding,) = by_kind(items, "ClusterRoleBinding").values()
    assert [s["name"] for s in binding["subjects"]] == ["pydanticos-mcp-worker"]
    g = grants(by_kind(items, "ClusterRole")[binding["roleRef"]["name"]])
    assert {r for _, r, _ in g} == {"namespaces", "rolebindings", "clusterroles"}
    assert {v for _, r, v in g if r == "namespaces"} == {"create", "get", "patch"}
    assert not {v for _, r, v in g if r == "namespaces"} & {"delete", "update", "list", "watch"}
    rules = by_kind(items, "ClusterRole")[binding["roleRef"]["name"]]["rules"]
    (bind,) = [r for r in rules if "clusterroles" in r["resources"]]
    assert bind["verbs"] == ["bind"]
    assert set(bind["resourceNames"]) == {"pydanticos-mcp-tenant", "pydanticos-mcp-tenant-read"}


def test_per_tenant_role_is_static_and_by_name_for_secrets() -> None:
    roles = by_kind(docs("rbac-mcp.yaml"), "ClusterRole")
    g = grants(roles["pydanticos-mcp-tenant"])
    assert {v for _, r, v in g if r == "secrets"} == {"create", "get", "update", "patch", "delete"}
    assert not any(r == "secrets" and v in {"list", "watch"} for _, r, v in g)
    assert {"mcpservers", "networkpolicies", "resourcequotas", "limitranges", "pods", "pods/log", "services"} <= {r for _, r, _ in g}
    assert ("", "services", "get") in g and not any(r == "services" and v != "get" for _, r, v in g)
    assert not any(r in {"namespaces", "rolebindings", "roles"} for _, r, _ in g)


def test_api_service_account_is_read_only_and_has_no_secret_access() -> None:
    items = docs("rbac-mcp.yaml")
    sas = by_kind(items, "ServiceAccount")
    assert {"pydanticos-mcp-worker", "pydanticos-mcp-api"} <= set(sas)
    g = grants(by_kind(items, "ClusterRole")["pydanticos-mcp-tenant-read"])
    assert {v for _, _, v in g} <= {"get", "list"}
    assert "secrets" not in {r for _, r, _ in g}
    assert ("", "pods/log", "get") in g
    # nothing binds the api account cluster wide
    for b in by_kind(items, "ClusterRoleBinding").values():
        assert "pydanticos-mcp-api" not in [s["name"] for s in b["subjects"]]


def test_admission_policy_confines_the_worker_to_mcp_tenant_namespaces() -> None:
    items = docs("rbac-mcp.yaml")
    (policy,) = by_kind(items, "ValidatingAdmissionPolicy").values()
    (binding,) = by_kind(items, "ValidatingAdmissionPolicyBinding").values()
    assert binding["spec"]["policyName"] == policy["metadata"]["name"]
    assert binding["spec"]["validationActions"] == ["Deny"]
    text = yaml.safe_dump(policy)
    assert "mcp-tenant-" in text and "system:serviceaccount:pydanticos:pydanticos-mcp-worker" in text
    resources = {r for rule in policy["spec"]["matchConstraints"]["resourceRules"] for r in rule["resources"]}
    assert resources == {"namespaces", "rolebindings"}


def test_workloads_use_their_own_service_accounts() -> None:
    (sts,) = [d for d in docs("worker.yaml") if d["kind"] == "StatefulSet"]
    (dep,) = [d for d in docs("api.yaml") if d["kind"] == "Deployment"]
    assert sts["spec"]["template"]["spec"]["serviceAccountName"] == "pydanticos-mcp-worker"
    assert dep["spec"]["template"]["spec"]["serviceAccountName"] == "pydanticos-mcp-api"


def test_api_server_addresses_are_substituted_by_up_sh() -> None:
    up = (ROOT / "deploy" / "scripts" / "up.sh").read_text()
    assert "get endpoints kubernetes" in up and "EAP_MCP_API_SERVER_CIDRS" in up and "EAP_MCP_API_SERVER_PORTS" in up
    assert re.search(r"get svc kubernetes", up)
    assert "EAP_MCP_API_SERVER_CIDRS" in (K8S / "configmap.yaml").read_text()
