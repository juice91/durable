"""IAM deployment configuration: the plug-in selection, the Access settings and the entrypoint."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kustomize_util import kustomize_docs

ROOT = Path(__file__).resolve().parents[2]
# The policy engine, the OpenFGA ids and the grant enforcement the shipped ConfigMap selects.
# How the shipped ConfigMap signs people in: identity chain, membership source, Studio mode.
IDENTITY, MEMBERSHIP, STUDIO_AUTH = "localadmin,apikey,devloopback", "localadmin", "local"
ISSUER = None  # the public tree's is the operator's replaced one: whatever the export wrote
POLICY, STORE_ID, MODEL_ID, GRANTS = "openaccess", "", "", "0"


def _config() -> dict:
    for doc in kustomize_docs():
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("-config") and "EAP_AUTH_MODE" in doc["data"]:
            return doc["data"]
    raise AssertionError("no application ConfigMap with EAP_AUTH_MODE")


def test_configmap_selects_the_transition_plugins():
    data = _config()
    assert data["EAP_AUTH_MODE"] == "dev"
    # F1: `cfaccess` is gone (every Access person became a `studio` user); the SSH tunnel's dev bearer stays.
    assert data["EAP_IAM_IDENTITY"] == IDENTITY
    assert data["EAP_IAM_POLICY"] == POLICY


def test_zitadel_is_wired_and_the_configmap_selects_the_sign_in():
    data = _config()
    assert data["EAP_IAM_MEMBERSHIP"] == MEMBERSHIP
    assert ISSUER is None or data["EAP_ZITADEL_ISSUER"] == ISSUER
    assert data["EAP_ZITADEL_API_URL"] == "http://zitadel.iam.svc.cluster.local:8080"
    assert data["EAP_STUDIO_AUTH"] == STUDIO_AUTH
    # None of the operator-supplied values is in git: they come from the Secret, optional so the deploy ships first.
    for name in ("EAP_ZITADEL_PROJECT_ID", "EAP_ZITADEL_ORG_TENANT_MAP", "EAP_ZITADEL_MACHINE_KEY", "EAP_ZITADEL_STUDIO_CLIENT_ID"):
        assert name not in data
    docs = {d["metadata"]["name"]: d for d in kustomize_docs() if d.get("kind") == "Deployment"}
    api_env = {e["name"]: e for e in docs["api"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    for name in ("EAP_ZITADEL_PROJECT_ID", "EAP_ZITADEL_ORG_TENANT_MAP", "EAP_ZITADEL_MACHINE_KEY"):
        ref = api_env[name]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "pydanticos-secrets" and ref["key"] == name and ref["optional"] is True
    studio_env = {e["name"]: e for e in docs["studio"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert studio_env["EAP_STUDIO_AUTH"]["valueFrom"]["configMapKeyRef"]["key"] == "EAP_STUDIO_AUTH"
    assert studio_env["EAP_STUDIO_ZITADEL_ISSUER"]["valueFrom"]["configMapKeyRef"]["key"] == "EAP_ZITADEL_ISSUER"
    for env_name, key in (("EAP_STUDIO_ZITADEL_CLIENT_ID", "EAP_ZITADEL_STUDIO_CLIENT_ID"), ("EAP_STUDIO_ZITADEL_PROJECT_ID", "EAP_ZITADEL_PROJECT_ID")):
        ref = studio_env[env_name]["valueFrom"]["secretKeyRef"]
        assert ref["key"] == key and ref["optional"] is True


def test_api_and_worker_may_reach_zitadel_on_8080_and_nothing_else_widens():
    policies = {d["metadata"]["name"]: d for d in kustomize_docs() if d.get("kind") == "NetworkPolicy"}
    policy = policies["iam-egress-zitadel"]
    names = policy["spec"]["podSelector"]["matchExpressions"][0]["values"]
    assert {"api", "worker"} <= set(names)
    (rule,) = policy["spec"]["egress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8080}]
    assert rule["to"][0]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "zitadel"}


def test_cfaccess_is_not_deployed_and_the_temporary_trust_path_is_gone():
    data = _config()
    assert "cfaccess" not in data["EAP_IAM_IDENTITY"]
    assert not [name for name in data if name.startswith("EAP_CF_ACCESS_")]
    assert data["EAP_CORS_LOOPBACK_ORIGINS"] == "http://127.0.0.1:13000,http://localhost:13000"
    assert data["EAP_AUDIT_RETENTION_DAYS"] == "400"


def test_the_deployed_chain_starts_and_answers_401_to_an_access_jwt_alone_while_the_dev_bearer_works(monkeypatch):
    """F1 against the plug-ins the ConfigMap selects: an Access JWT identifies nobody, the tunnel's bearer still does."""
    import asyncio

    from eap_iam import AuthError, IdentityChain, PluginContext, RequestView
    from eap_iam.registry import installed

    data = _config()
    monkeypatch.setenv("EAP_AUTH_MODE", "dev")
    factories = installed("eap.iam.identity", {})
    names = data["EAP_IAM_IDENTITY"].split(",")
    chain = IdentityChain([factories[name](PluginContext(engine=None, env=dict(data))) for name in names if name not in ("apikey", "localadmin")])  # those need a database

    def ask(headers, peer):
        return asyncio.run(chain.authenticate(RequestView(headers=headers, peer_host=peer, accepts=frozenset({"user"}))))

    with pytest.raises(AuthError) as refused:
        ask({"cf-access-jwt-assertion": "a.b.c"}, "10.42.0.9")
    assert refused.value.status == 401
    principal = ask({"authorization": "Bearer dev:studio:operator"}, "127.0.0.1")
    assert (principal.tenant_id, principal.subject, principal.provider) == ("studio", "operator", "loopback-dev")


def test_the_api_runs_without_proxy_headers_and_migrate_runs_the_plugin_branches():
    entrypoint = (ROOT / "deploy" / "docker" / "python-entrypoint.sh").read_text()
    api = entrypoint.split("api)", 1)[1].split(";;", 1)[0]
    assert "--no-proxy-headers" in api and "--proxy-headers" not in api.replace("--no-proxy-headers", "")
    migrate = entrypoint.split("migrate)", 1)[1].split(";;", 1)[0]
    assert migrate.index("alembic -c packages/db/alembic.ini upgrade head") < migrate.index("eap-db plugins upgrade")


def test_the_plugins_are_workspace_members_installed_into_the_image():
    assert "plugins/*" in (ROOT / "pyproject.toml").read_text()
    assert "uv sync --all-packages" in (ROOT / "deploy" / "docker" / "python.Dockerfile").read_text()


def test_studio_gets_the_tunnel_api_url_at_runtime_not_at_build():
    studio = next(d for d in kustomize_docs() if d.get("kind") == "Deployment" and d["metadata"]["name"] == "studio")
    env = {e["name"]: e.get("value") for e in studio["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["EAP_STUDIO_LOOPBACK_API_URL"] == "http://127.0.0.1:18000"
    assert "EAP_STUDIO_LOOPBACK_API_URL" not in (ROOT / "deploy" / "docker" / "studio.Dockerfile").read_text()


def test_studio_serves_the_dev_bearer_at_runtime_to_loopback_only_never_at_build():
    dockerfile = (ROOT / "deploy" / "docker" / "studio.Dockerfile").read_text()
    assert "NEXT_PUBLIC_EAP_DEV_TOKEN" not in dockerfile and "Bearer dev:" not in dockerfile
    studio = next(d for d in kustomize_docs() if d.get("kind") == "Deployment" and d["metadata"]["name"] == "studio")
    env = {e["name"]: e.get("value") for e in studio["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["EAP_STUDIO_LOOPBACK_DEV_BEARER"] == "Bearer dev:studio:operator"


# ---- OpenFGA wiring for the API and the worker (IAM P2c): present and active since the switch to openfga ----


def _workload_env(name: str) -> dict:
    (doc,) = [d for d in kustomize_docs() if d.get("kind") in ("Deployment", "StatefulSet") and d["metadata"]["name"] == name]
    (container,) = doc["spec"]["template"]["spec"]["containers"]
    assert {"configMapRef": {"name": "pydanticos-config"}} in container["envFrom"], name
    return {e["name"]: e for e in container["env"]}


def test_openfga_settings_are_in_the_configmap():
    data = _config()
    assert data["EAP_OPENFGA_URL"] == "http://openfga.iam.svc.cluster.local:8080"
    assert data["EAP_OPENFGA_STORE_ID"] == STORE_ID
    assert data["EAP_OPENFGA_MODEL_ID"] == MODEL_ID
    assert data["EAP_AGENT_GRANTS_ENFORCE"] == GRANTS
    assert "EAP_OPENFGA_PRESHARED_KEY" not in data  # a credential: Secret only


def test_openfga_defaults_in_the_configmap_are_the_plugins_own_defaults():
    from dataclasses import replace

    from eap_iam_openfga.settings import load_settings

    data = _config()
    deployed = load_settings({**data, "EAP_OPENFGA_STORE_ID": "store"})
    defaults = load_settings({"EAP_OPENFGA_URL": data["EAP_OPENFGA_URL"], "EAP_OPENFGA_STORE_ID": "store"})
    # The pinned model id is the one value the operator sets beyond the plug-in's own defaults (empty: not pinned).
    assert deployed == replace(defaults, model_id=data["EAP_OPENFGA_MODEL_ID"] or None)


def test_the_selected_policy_loads_and_grant_enforcement_follows_the_switch():
    from eap_iam import enforcement_active, load_policy

    data = _config()
    engine = load_policy(env={**data, "EAP_IAM_IGNORE_PLUGINS": ""})
    assert engine.name == POLICY
    assert enforcement_active(engine, data) is (GRANTS == "1")


def test_selecting_openfga_before_the_bootstrap_refuses_to_start():
    from eap_iam import IamConfigError, load_policy

    with pytest.raises(IamConfigError, match="EAP_OPENFGA_STORE_ID"):
        load_policy(env={**_config(), "EAP_IAM_POLICY": "openfga", "EAP_OPENFGA_STORE_ID": ""})


def test_api_and_worker_read_the_preshared_key_openfga_is_configured_with_from_the_platform_secret():
    for name in ("api", "worker"):
        ref = _workload_env(name)["EAP_OPENFGA_PRESHARED_KEY"]["valueFrom"]["secretKeyRef"]
        # The same field OpenFGA's own Secret takes it from, so the two cannot differ.
        assert ref == {"name": "pydanticos-secrets", "key": "OPENFGA_PRESHARED_KEY", "optional": True}
    readers = [
        d["metadata"]["name"]
        for d in kustomize_docs()
        if d.get("kind") in ("Deployment", "StatefulSet", "Job", "CronJob") and "OPENFGA_PRESHARED_KEY" in yaml.safe_dump(d)
    ]
    assert sorted(readers) == ["api", "worker"]
    up = (ROOT / "deploy/scripts/up.sh").read_text()
    platform_checksum = next(line for line in up.splitlines() if line.startswith("checksum_platform="))
    assert "OPENFGA_PRESHARED_KEY" in platform_checksum  # a rotated key restarts the API and the worker


def test_only_api_and_worker_may_reach_openfga():
    policies = {d["metadata"]["name"]: d for d in kustomize_docs() if d.get("kind") == "NetworkPolicy"}
    reaching = [
        (name, p["spec"])
        for name, p in policies.items()
        for rule in p["spec"].get("egress", [])
        for to in rule.get("to", [])
        if to.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name") == "openfga"
    ]
    ((name, spec),) = reaching
    assert name == "iam-egress-openfga"
    assert spec["podSelector"]["matchExpressions"] == [{"key": "app.kubernetes.io/name", "operator": "In", "values": ["api", "worker"]}]
    assert {"protocol": "TCP", "port": 8080} in spec["egress"][0]["ports"]


def test_api_and_worker_run_one_image_that_ships_the_openfga_plugin_and_the_operator_clis():
    images = {
        c["image"]
        for d in kustomize_docs()
        if d.get("kind") in ("Deployment", "StatefulSet") and d["metadata"]["name"] in ("api", "worker")
        for c in d["spec"]["template"]["spec"]["containers"]
    }
    assert len(images) == 1 and images.pop().split(":")[0] == "pydanticos-python"
    dockerfile = (ROOT / "deploy/docker/python.Dockerfile").read_text()
    assert "uv sync --all-packages --frozen --no-dev" in dockerfile and "COPY . ." in dockerfile
    assert '"plugins/*"' in (ROOT / "pyproject.toml").read_text()
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert not [p for p in ignored if p.strip("*/").split("/")[0] in {"plugins", "scripts"}]
    assert (ROOT / "scripts/iam/backfill_agent_grants.py").is_file()


def test_the_runbook_names_every_step_of_the_switch_and_the_rollback():
    text = (ROOT / "docs/iam.md").read_text()
    section = text.split("## Switching to OpenFGA", 1)[1].split("\n## ", 1)[0]
    needles = (
        "python -m eap_iam_openfga.bootstrap --dry-run",
        "EAP_OPENFGA_STORE_ID",
        "EAP_OPENFGA_MODEL_ID",
        "python -m eap_iam_openfga.backfill",
        "python -m eap_iam_openfga.reconcile",
        "EAP_IAM_POLICY: openfga",
        "/app/scripts/iam/backfill_agent_grants.py",
        "EAP_AGENT_GRANTS_ENFORCE",
        "### Rollback",
        "EAP_IAM_POLICY: openaccess",
    )
    for needle in needles:
        assert needle in section, needle
    # In this order: the ids, the dry run, the switch, then the agent grants.
    order = [section.index(n) for n in ("bootstrap --dry-run", "eap_iam_openfga.backfill", "EAP_IAM_POLICY: openfga", "backfill_agent_grants.py")]
    assert order == sorted(order)
