"""Identity (ZITADEL) and authorization (OpenFGA): deploy/k8s/iam, up.sh wiring, NetworkPolicy shape, backups."""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

from kustomize_util import K8S_DIR, WORKLOAD_KINDS, containers, kustomize_docs, pod_spec

ROOT = K8S_DIR.parents[1]
IAM_DIR = K8S_DIR / "iam"
DIGEST_IMAGE = r"[a-z0-9./-]+:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}"
SECRET_FIELDS = {
    "ZITADEL_MASTERKEY",
    "ZITADEL_DB_PASSWORD",
    "ZITADEL_ADMIN_USERNAME",
    "ZITADEL_ADMIN_PASSWORD",
    "OPENFGA_DB_PASSWORD",
    "OPENFGA_PRESHARED_KEY",
}


@lru_cache(maxsize=1)
def iam_docs() -> tuple[dict, ...]:
    out = subprocess.run(["kubectl", "kustomize", str(IAM_DIR)], check=True, capture_output=True, text=True).stdout
    return tuple(d for d in yaml.safe_load_all(out) if d)


def _one(kind: str, name: str, docs=None) -> dict:
    found = [d for d in (docs or iam_docs()) if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, (kind, name)
    return found[0]


def _policies(docs) -> dict[str, dict]:
    return {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "NetworkPolicy"}


def _workloads():
    return [d for d in iam_docs() if d["kind"] in WORKLOAD_KINDS]


def test_everything_lives_in_the_iam_namespace_and_the_platform_kustomization_has_none_of_it():
    for doc in iam_docs():
        if doc["kind"] != "Namespace":
            assert doc["metadata"]["namespace"] == "iam", doc["metadata"]["name"]
    assert _one("Namespace", "iam")
    platform = {(d["kind"], d["metadata"]["name"]) for d in kustomize_docs()}
    assert ("Deployment", "zitadel") not in platform and ("Deployment", "openfga") not in platform


@pytest.mark.parametrize("name,repo", [("zitadel", "ghcr.io/zitadel/zitadel"), ("openfga", "openfga/openfga")])
def test_images_are_pinned_by_version_and_digest(name, repo):
    spec = pod_spec(_one("Deployment", name))
    images = {c["image"] for c in containers(_one("Deployment", name))}
    assert images and all(re.fullmatch(DIGEST_IMAGE, i) and i.startswith(repo + ":") for i in images), images
    assert len(images) == 1, "init containers and the server must run the same pinned image"
    assert spec["automountServiceAccountToken"] is False


def test_both_deployments_are_hardened_sized_probed_and_off_by_default():
    for name, uid in (("zitadel", 1000), ("openfga", 65532)):
        doc = _one("Deployment", name)
        assert doc["spec"]["replicas"] == 0  # up.sh scales to 1 only when the deploy secret has the fields
        spec = pod_spec(doc)
        pod_sec = spec["securityContext"]
        assert pod_sec["runAsNonRoot"] is True and pod_sec["runAsUser"] == uid and pod_sec["runAsGroup"] == uid
        assert pod_sec["seccompProfile"]["type"] == "RuntimeDefault"
        for c in containers(doc):
            sec = c["securityContext"]
            assert sec["runAsNonRoot"] is True and sec["runAsUser"] == uid and sec["runAsGroup"] == uid
            assert sec["allowPrivilegeEscalation"] is False and sec["readOnlyRootFilesystem"] is True
            assert "ALL" in sec["capabilities"]["drop"]
            res = c["resources"]
            assert res["requests"]["cpu"] and res["requests"]["memory"] and res["limits"]["cpu"] and res["limits"]["memory"]
        main = spec["containers"][0]
        assert main["readinessProbe"] and main["livenessProbe"]


def test_every_secret_reference_is_optional_and_comes_from_the_components_own_secret():
    # One Secret per component (up.sh creates them from the deploy secrets).
    own = {"zitadel": "zitadel-secrets", "openfga": "openfga-secrets"}
    for doc in _workloads():
        for c in containers(doc):
            for env in c.get("env", []):
                ref = (env.get("valueFrom") or {}).get("secretKeyRef")
                if ref:
                    assert ref["name"] == own[doc["metadata"]["name"]] and ref["key"] in SECRET_FIELDS and ref.get("optional") is True
                else:
                    # No secret value is ever written into the manifest.
                    assert not re.search(r"(?i)(key|password|token)$", env["name"]) or "value" not in env or env["value"] in {"", "zitadel"}, env["name"]


def test_openfga_flow_preshared_key_no_playground_metrics_and_cluster_ip_only():
    doc = _one("Deployment", "openfga")
    spec = pod_spec(doc)
    assert [c["args"] for c in spec["initContainers"]] == [["migrate"]]
    main = spec["containers"][0]
    assert main["args"] == ["run"]
    env = {e["name"]: e for e in main["env"]}
    assert env["OPENFGA_DATASTORE_ENGINE"]["value"] == "postgres"
    assert env["OPENFGA_AUTHN_METHOD"]["value"] == "preshared"
    assert env["OPENFGA_AUTHN_PRESHARED_KEYS"]["valueFrom"]["secretKeyRef"]["key"] == "OPENFGA_PRESHARED_KEY"
    assert env["OPENFGA_PLAYGROUND_ENABLED"]["value"] == "false"
    assert env["OPENFGA_METRICS_ENABLED"]["value"] == "true"
    # Expanded by Kubernetes, so the password is in the pod environment only, never in a manifest.
    uri = env["OPENFGA_DATASTORE_URI"]["value"]
    assert uri.startswith("postgres://openfga:$(OPENFGA_DB_PASSWORD)@postgres.pydanticos.svc.cluster.local:5432/openfga")
    order = [e["name"] for e in main["env"]]
    assert order.index("OPENFGA_DB_PASSWORD") < order.index("OPENFGA_DATASTORE_URI")
    assert main["readinessProbe"]["httpGet"]["path"] == "/healthz" and main["livenessProbe"]["httpGet"]["path"] == "/healthz"
    svc = _one("Service", "openfga")["spec"]
    assert svc["type"] == "ClusterIP"
    assert {(p["name"], p["port"]) for p in svc["ports"]} == {("http", 8080), ("grpc", 8081)}


def test_zitadel_flow_init_setup_start_external_tls_and_first_instance():
    doc = _one("Deployment", "zitadel")
    spec = pod_spec(doc)
    init, setup = spec["initContainers"]
    assert init["args"] == ["init"]
    assert setup["args"] == ["setup", "--masterkeyFromEnv", "--tlsMode", "external"]
    main = spec["containers"][0]
    assert main["args"] == ["start", "--masterkeyFromEnv", "--tlsMode", "external"]
    cfg = _one("ConfigMap", "zitadel-config")["data"]
    assert cfg["ZITADEL_EXTERNALDOMAIN"] == "auth.example-org.ai"
    assert cfg["ZITADEL_EXTERNALPORT"] == "443" and cfg["ZITADEL_EXTERNALSECURE"] == "true"
    assert cfg["ZITADEL_DATABASE_POSTGRES_HOST"] == "postgres.pydanticos.svc.cluster.local"
    assert cfg["ZITADEL_DATABASE_POSTGRES_DATABASE"] == "zitadel" and cfg["ZITADEL_DATABASE_POSTGRES_USER_USERNAME"] == "zitadel"
    # v4 defaults to Login V2 (served only by the zitadel-login container, which we do not run): pin the v1 login.
    assert cfg["ZITADEL_DEFAULTINSTANCE_FEATURES_LOGINV2_REQUIRED"] == "false"
    assert cfg["ZITADEL_FIRSTINSTANCE_ORG_NAME"] == "Durable"
    assert cfg["ZITADEL_FIRSTINSTANCE_ORG_HUMAN_PASSWORDCHANGEREQUIRED"] == "true"
    assert cfg["ZITADEL_FIRSTINSTANCE_MACHINEKEYPATH"] == "/bootstrap/machine-key.json"
    # Every container reads the master key from the Secret, never from the ConfigMap or an argument.
    for c in (setup, main):
        env = {e["name"]: e for e in c["env"]}
        assert env["ZITADEL_MASTERKEY"]["valueFrom"]["secretKeyRef"]["key"] == "ZITADEL_MASTERKEY"
    assert not any(k.endswith(("MASTERKEY", "_PASSWORD")) for k in cfg)
    setup_env = {e["name"]: e for e in setup["env"]}
    assert setup_env["ZITADEL_FIRSTINSTANCE_ORG_HUMAN_USERNAME"]["valueFrom"]["secretKeyRef"]["key"] == "ZITADEL_ADMIN_USERNAME"
    assert setup_env["ZITADEL_FIRSTINSTANCE_ORG_HUMAN_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "ZITADEL_ADMIN_PASSWORD"
    # The machine key lands on a volume that only the setup step mounts; nothing logs it.
    assert [m["mountPath"] for m in setup["volumeMounts"]] == ["/bootstrap"]
    assert "volumeMounts" not in main
    assert main["readinessProbe"]["httpGet"] == {"path": "/debug/ready", "port": "http"}
    assert main["livenessProbe"]["httpGet"] == {"path": "/debug/healthz", "port": "http"}
    svc = _one("Service", "zitadel")["spec"]
    assert svc["type"] == "ClusterIP" and [(p["port"], p["appProtocol"]) for p in svc["ports"]] == [(8080, "kubernetes.io/h2c")]


def test_iam_namespace_policies_default_deny_and_admit_only_the_named_peers():
    pol = _policies(iam_docs())
    assert pol["default-deny"]["podSelector"] == {} and set(pol["default-deny"]["policyTypes"]) == {"Ingress", "Egress"}
    pg = pol["iam-egress-postgres"]["egress"]
    assert len(pg) == 1 and pg[0]["to"][0]["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "pydanticos"}
    assert pg[0]["to"][0]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "postgres"}
    assert [p["port"] for p in pg[0]["ports"]] == [5432]
    assert pol["iam-egress-postgres"]["podSelector"]["matchExpressions"][0]["values"] == ["zitadel", "openfga"]
    for name, target, names, ports in (
        ("zitadel-ingress", "zitadel", ["api", "worker"], [8080]),
        ("openfga-ingress", "openfga", ["api", "worker"], [8080, 8081]),
    ):
        spec = pol[name]
        assert spec["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": target}
        assert spec["policyTypes"] == ["Ingress"] and len(spec["ingress"]) == 1
        rule = spec["ingress"][0]
        assert len(rule["from"]) == 1
        peer = rule["from"][0]
        assert peer["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "pydanticos"}
        assert peer["podSelector"]["matchExpressions"][0]["values"] == names
        assert [p["port"] for p in rule["ports"]] == ports
    # Nothing in iam reaches the internet: the only egress rules are DNS and Postgres.
    egress_policies = {n for n, s in pol.items() if "Egress" in s["policyTypes"]}
    assert egress_policies == {"default-deny", "allow-dns", "iam-egress-postgres"}


def test_platform_policies_reach_into_iam_and_every_older_policy_stays():
    pol = _policies(kustomize_docs())
    ingress = pol["postgres-ingress-iam"]
    peer = ingress["ingress"][0]["from"][0]
    assert peer["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "iam"}
    assert peer["podSelector"]["matchExpressions"][0]["values"] == ["zitadel", "openfga"]
    assert [p["port"] for p in ingress["ingress"][0]["ports"]] == [5432]
    for name, sources, target, ports in (
        ("iam-egress-zitadel", ["api", "worker"], "zitadel", [8080]),
        ("iam-egress-openfga", ["api", "worker"], "openfga", [8080, 8081]),
    ):
        spec = pol[name]
        assert spec["policyTypes"] == ["Egress"]
        assert spec["podSelector"]["matchExpressions"][0]["values"] == sources
        to = spec["egress"][0]["to"][0]
        assert to["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "iam"}
        assert to["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": target}
        assert [p["port"] for p in spec["egress"][0]["ports"]] == ports
    # The platform's own Postgres ingress is untouched: the iam peers are a separate policy.
    old = {p["podSelector"]["matchLabels"]["app.kubernetes.io/name"] for p in pol["postgres-ingress"]["ingress"][0]["from"]}
    assert old == {"api", "worker", "migrate", "postgres-backup"}
    assert {"default-deny", "allow-dns-and-otlp", "api-ingress-worker", "api-ingress-studio"} <= set(pol)


def test_zitadel_bootstrap_volume_is_a_small_pvc():
    pvc = _one("PersistentVolumeClaim", "zitadel-bootstrap")["spec"]
    assert pvc["accessModes"] == ["ReadWriteOnce"] and pvc["resources"]["requests"]["storage"] == "16Mi"


# ---- deploy wiring -------------------------------------------------------------------------------------------------


def test_secret_fields_are_declared_in_the_example():
    example = {ln.split("=", 1)[0] for ln in (K8S_DIR / "secrets.example.env").read_text().splitlines() if "=" in ln and not ln.startswith("#")}
    assert SECRET_FIELDS <= example




def test_up_sh_wires_the_optional_services_without_echoing_values():
    up = (ROOT / "deploy/scripts/up.sh").read_text()
    assert "scale deployment/zitadel --replicas=1" in up and "scale deployment/zitadel --replicas=0" in up
    assert "scale deployment/openfga --replicas=1" in up and "scale deployment/openfga --replicas=0" in up
    assert "ZITADEL_MASTERKEY must be exactly 32 characters" in up
    # The password reaches psql through stdin, never a command line.
    assert "exec -i postgres-0" in up and 'read -r pw' in up
    # The platform Secret never carries the master key or the admin credentials. (It does carry OpenFGA's preshared key,
    # which the API and the worker present to OpenFGA.)
    assert "^(ZITADEL_MASTERKEY|ZITADEL_ADMIN_USERNAME|ZITADEL_ADMIN_PASSWORD)=" in up


def test_backup_job_dumps_the_identity_databases_when_their_passwords_are_present():
    script = (K8S_DIR / "backup-job.sh").read_text()
    assert 'publish_dump zitadel zitadel "$ZITADEL_DB_PASSWORD"' in script
    assert 'publish_dump openfga openfga "$OPENFGA_DB_PASSWORD"' in script
    assert "for db in eap_app eap_dbos zitadel openfga; do" in script
    cron = next(d for d in kustomize_docs() if d["kind"] == "CronJob" and d["metadata"]["name"] == "postgres-backup")
    env = {e["name"]: e for e in next(c for c in containers(cron) if c["name"] == "pg-dump")["env"]}
    for key in ("ZITADEL_DB_PASSWORD", "OPENFGA_DB_PASSWORD"):
        assert env[key]["valueFrom"]["secretKeyRef"] == {"name": "pydanticos-secrets", "key": key, "optional": True}




def test_iam_workloads_disable_service_links():
    # Kubernetes injects ZITADEL_PORT=tcp://... for the Service named zitadel; ZITADEL reads every ZITADEL_* variable
    # as configuration and crashes. OpenFGA reads OPENFGA_* the same way.
    workloads = [d for d in iam_docs() if d["kind"] in WORKLOAD_KINDS]
    assert workloads
    for doc in workloads:
        assert pod_spec(doc).get("enableServiceLinks") is False, doc["metadata"]["name"]
