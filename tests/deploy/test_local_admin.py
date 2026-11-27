"""The built-in admin account's deploy settings: the public tree signs in with it by default, the operator's does not."""

from __future__ import annotations

from kustomize_util import kustomize_docs

NAMES = ("EAP_LOCAL_ADMIN_USERNAME", "EAP_LOCAL_ADMIN_INITIAL_PASSWORD", "EAP_LOCAL_AUTH_SIGNING_KEY")


def _config() -> dict:
    return next(d["data"] for d in kustomize_docs() if d.get("kind") == "ConfigMap" and "EAP_AUTH_MODE" in d.get("data", {}))


def _env(deployment: str) -> dict:
    doc = next(d for d in kustomize_docs() if d.get("kind") == "Deployment" and d["metadata"]["name"] == deployment)
    return {e["name"]: e for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]}




def test_the_default_is_the_built_in_admin_with_its_secrets_wired_and_empty():
    from pathlib import Path

    data = _config()
    assert (data["EAP_IAM_IDENTITY"], data["EAP_IAM_MEMBERSHIP"], data["EAP_STUDIO_AUTH"]) == (
        "localadmin,apikey,devloopback", "localadmin", "local")
    # No value of the account is in the ConfigMap: all three come from the Secret.
    assert not [key for key in data if key.startswith("EAP_LOCAL_")]
    api = _env("api")
    for name in NAMES:
        ref = api[name]["valueFrom"]["secretKeyRef"]
        assert (ref["name"], ref["key"], ref["optional"]) == ("pydanticos-secrets", name, True)
    ref = _env("studio")["EAP_LOCAL_ADMIN_USERNAME"]["valueFrom"]["secretKeyRef"]
    assert ref["key"] == "EAP_LOCAL_ADMIN_USERNAME"
    assert not {"EAP_LOCAL_ADMIN_INITIAL_PASSWORD", "EAP_LOCAL_AUTH_SIGNING_KEY"} & set(_env("studio"))
    root = Path(__file__).resolve().parents[2]
    example = (root / "deploy/k8s/secrets.example.env").read_text().splitlines()
    template = (root / "deploy/k8s/secret.template.yaml").read_text()
    up = (root / "deploy/scripts/up.sh").read_text()
    for name in NAMES:
        assert f"{name}=" in example
        assert f'{name}: ""' in template
        assert name in next(line for line in up.splitlines() if line.startswith("checksum_platform="))
