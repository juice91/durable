"""IAM deployment configuration: the plug-in selection, the Access settings and the entrypoint."""

from __future__ import annotations

from pathlib import Path

from kustomize_util import kustomize_docs

ROOT = Path(__file__).resolve().parents[2]


def _config() -> dict:
    for doc in kustomize_docs():
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("-config") and "EAP_AUTH_MODE" in doc["data"]:
            return doc["data"]
    raise AssertionError("no application ConfigMap with EAP_AUTH_MODE")


def test_configmap_selects_the_transition_plugins():
    data = _config()
    assert data["EAP_AUTH_MODE"] == "dev"
    assert data["EAP_IAM_IDENTITY"] == "zitadel,apikey,cfaccess,devloopback"  # IAM P1: zitadel first, cfaccess for the transition
    assert data["EAP_IAM_POLICY"] == "openaccess"


def test_zitadel_is_wired_and_studio_signs_in_through_zitadel():
    data = _config()
    assert data["EAP_IAM_MEMBERSHIP"] == "zitadel"
    assert data["EAP_ZITADEL_ISSUER"] == "https://auth.example-org.ai"
    assert data["EAP_ZITADEL_API_URL"] == "http://zitadel.iam.svc.cluster.local:8080"
    assert data["EAP_STUDIO_AUTH"] == "zitadel"
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


def test_cfaccess_is_configured_and_the_temporary_trust_path_is_gone():
    data = _config()
    assert data["EAP_CF_ACCESS_TEAM_DOMAIN"] == "example-org.cloudflareaccess.com"
    assert data["EAP_CF_ACCESS_AUD"] == "REPLACE_WITH_ACCESS_APP_AUD"
    assert data["EAP_CF_ACCESS_TENANT"] == "studio"
    assert "EAP_IAM_DEVLOOPBACK_TRUST_CLOUDFLARED" not in data
    assert data["EAP_CORS_LOOPBACK_ORIGINS"] == "http://127.0.0.1:13000,http://localhost:13000"
    assert data["EAP_AUDIT_RETENTION_DAYS"] == "400"


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
