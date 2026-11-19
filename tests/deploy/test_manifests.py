"""Manifest checks from `kubectl kustomize deploy/k8s`."""

from __future__ import annotations

import yaml

from kustomize_util import LONG_RUNNING, WORKLOAD_KINDS, K8S_DIR, containers, kustomize_docs, pod_spec

COMPOSE_FLAGS = [
    "max_connections=300",
    "shared_buffers=512MB",
    "work_mem=16MB",
    "maintenance_work_mem=128MB",
    "effective_cache_size=2GB",
    "idle_in_transaction_session_timeout=300000",
]

WORKLOAD_UIDS = {
    ("Deployment", "api"): 10001,
    ("Deployment", "lgtm"): 1000,
    ("Deployment", "studio"): 10001,
    ("StatefulSet", "postgres"): 999,
    ("StatefulSet", "worker"): 10001,
    ("CronJob", "postgres-backup"): 999,
    ("Job", "migrate"): 10001,
}


def _workloads():
    return [d for d in kustomize_docs() if d.get("kind") in WORKLOAD_KINDS]


def test_every_workload_has_requests_and_limits():
    missing = []
    for doc in _workloads():
        name = doc["metadata"]["name"]
        for c in containers(doc):
            res = c.get("resources") or {}
            req, lim = res.get("requests") or {}, res.get("limits") or {}
            if not req.get("cpu") or not req.get("memory") or not lim.get("cpu") or not lim.get("memory"):
                missing.append(f"{doc['kind']}/{name}/{c.get('name')}")
    assert missing == []


def test_long_running_workloads_have_readiness_and_liveness_probes():
    missing = []
    for doc in _workloads():
        if doc["kind"] not in LONG_RUNNING:
            continue
        name = doc["metadata"]["name"]
        for c in containers(doc):
            if not c.get("readinessProbe") or not c.get("livenessProbe"):
                missing.append(f"{doc['kind']}/{name}/{c.get('name')}")
    assert missing == []


def test_every_workload_runs_non_root():
    missing = []
    for doc in _workloads():
        name = doc["metadata"]["name"]
        spec = pod_spec(doc)
        expected_uid = WORKLOAD_UIDS[(doc["kind"], name)]
        pod_security = spec.get("securityContext") or {}
        if (
            pod_security.get("runAsNonRoot") is not True
            or pod_security.get("runAsUser") != expected_uid
            or pod_security.get("runAsGroup") != expected_uid
            or (pod_security.get("seccompProfile") or {}).get("type") != "RuntimeDefault"
        ):
            missing.append(f"{doc['kind']}/{name} pod")
        for c in containers(doc):
            security = c.get("securityContext") or {}
            if (
                security.get("runAsNonRoot") is not True
                or security.get("runAsUser") != expected_uid
                or security.get("runAsGroup") != expected_uid
                or security.get("allowPrivilegeEscalation") is not False
                or "ALL" not in (security.get("capabilities") or {}).get("drop", [])
            ):
                missing.append(f"{doc['kind']}/{name}/{c.get('name')}")
    assert missing == [], f"workloads missing required non-root security context: {missing}"


def test_workload_root_filesystems_are_read_only_except_postgres():
    writable = []
    for doc in _workloads():
        name = doc["metadata"]["name"]
        for c in containers(doc):
            read_only = (c.get("securityContext") or {}).get("readOnlyRootFilesystem")
            if (doc["kind"], name, c.get("name")) == ("StatefulSet", "postgres", "postgres"):
                assert read_only is False
            elif read_only is not True:
                writable.append(f"{doc['kind']}/{name}/{c.get('name')}")
    assert writable == [], f"workloads with unexpectedly writable root filesystems: {writable}"


def test_services_are_clusterip_only_and_no_ingress():
    bad = []
    for doc in kustomize_docs():
        kind, name = doc.get("kind"), doc.get("metadata", {}).get("name")
        if kind == "Ingress":
            bad.append(f"Ingress/{name}")
        if kind == "Service":
            stype = doc.get("spec", {}).get("type", "ClusterIP")
            if stype not in {"ClusterIP", None}:
                bad.append(f"Service/{name} type={stype}")
    assert bad == []


def test_worker_is_statefulset_with_executor_id_from_pod_name():
    workers = [d for d in kustomize_docs() if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "worker"]
    assert len(workers) == 1
    env = {e["name"]: e for c in containers(workers[0]) for e in c.get("env") or []}
    field = env["EAP_EXECUTOR_ID"]["valueFrom"]["fieldRef"]["fieldPath"]
    assert field == "metadata.name"


def test_migrate_job_runs_migrate_and_is_suspended():
    jobs = [d for d in kustomize_docs() if d["kind"] == "Job" and d["metadata"]["name"] == "migrate"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["spec"].get("suspend") is True
    args = containers(job)[0].get("args") or []
    assert "migrate" in args


def test_postgres_matches_dev_tuning_pgvector_and_shm():
    docs = [d for d in kustomize_docs() if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "postgres"]
    assert len(docs) == 1
    pg = docs[0]
    c = containers(pg)[0]
    assert c["image"].startswith("pgvector/pgvector:") and "pg17" in c["image"]
    args = c.get("args") or []
    for flag in COMPOSE_FLAGS:
        assert flag in args, flag
    volumes = {v["name"]: v for v in pod_spec(pg).get("volumes") or []}
    shm = volumes["dshm"]["emptyDir"]
    assert shm.get("medium") == "Memory"
    mounts = {m["mountPath"]: m for m in c.get("volumeMounts") or []}
    assert mounts["/dev/shm"]["name"] == "dshm"


def test_litellm_url_defaults_to_host_lima_internal_4001():
    cms = [d for d in kustomize_docs() if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "pydanticos-config"]
    assert len(cms) == 1
    data = cms[0]["data"]
    for key in ("EAP_LITELLM_BASE_URL", "EAP_LITELLM_EMBEDDING_BASE_URL"):
        assert data[key] == "http://host.lima.internal:4001", (key, data[key])


def test_external_otlp_forwarding_is_off_by_default_and_the_limit_is_set():
    cm = next(d for d in kustomize_docs() if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "pydanticos-config")
    data = cm["data"]
    assert data["EAP_OTLP_FORWARD_ENDPOINT"] == ""  # off: set to http://lgtm:4318/v1/traces to forward
    assert int(data["EAP_OTLP_RATE_LIMIT_PER_MINUTE"]) > 0
    assert data["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://lgtm:4318"


def test_r2_endpoint_is_in_the_configmap_not_a_secret():
    cm = next(d for d in kustomize_docs() if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "pydanticos-config")
    data = cm["data"]
    assert data["EAP_BLOB_S3_ENDPOINT"] == "https://9a497744238ab16a1060dc3ae8abd102.r2.cloudflarestorage.com"
    assert data["EAP_BLOB_S3_BUCKET"] == "pydanticos-attachments"
    assert data["EAP_BLOB_S3_REGION"] == "auto"
    assert "PLACEHOLDER" not in data["EAP_BLOB_S3_ENDPOINT"]


def test_kustomize_emits_no_secret_objects():
    secrets = [d for d in kustomize_docs() if d.get("kind") == "Secret"]
    assert secrets == []


def _policies():
    return [d for d in kustomize_docs() if d.get("kind") == "NetworkPolicy"]


def _policy(name: str) -> dict:
    found = [d for d in _policies() if d["metadata"]["name"] == name]
    assert len(found) == 1, name
    return found[0]


def test_default_deny_ingress_and_egress():
    spec = _policy("default-deny")["spec"]
    assert spec.get("podSelector") == {}
    assert set(spec.get("policyTypes") or []) == {"Ingress", "Egress"}
    assert not spec.get("ingress")
    assert not spec.get("egress")


def test_dns_egress_targets_kube_dns():
    egress = _policy("allow-dns-and-otlp")["spec"]["egress"]
    dns = egress[0]
    peer = dns["to"][0]
    assert peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "kube-system"
    assert peer["podSelector"]["matchLabels"]["k8s-app"] == "kube-dns"
    ports = {(p.get("protocol"), p.get("port")) for p in dns["ports"]}
    assert ports == {("UDP", 53), ("TCP", 53)}


def test_every_pod_may_send_otlp_to_lgtm():
    egress = _policy("allow-dns-and-otlp")["spec"]["egress"]
    otlp = egress[1]
    assert otlp["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "lgtm"
    ports = {(p.get("protocol"), p.get("port")) for p in otlp["ports"]}
    assert ports == {("TCP", 4317), ("TCP", 4318)}
    ingress = _policy("lgtm-otlp-ingress")["spec"]["ingress"][0]
    assert ingress["from"][0]["podSelector"] == {}
    in_ports = {(p.get("protocol"), p.get("port")) for p in ingress["ports"]}
    assert in_ports == {("TCP", 4317), ("TCP", 4318)}


def test_postgres_clients_are_api_worker_migrate_and_backup_not_studio():
    peers = _policy("postgres-ingress")["spec"]["ingress"][0]["from"]
    names = {p["podSelector"]["matchLabels"]["app.kubernetes.io/name"] for p in peers}
    assert names == {"api", "worker", "migrate", "postgres-backup"}
    assert "studio" not in names
    egress_policy = _policy("postgres-clients-egress")["spec"]
    selected = set(egress_policy["podSelector"]["matchExpressions"][0]["values"])
    assert selected == names


def test_studio_reaches_only_the_api():
    egress = _policy("studio-egress-api")["spec"]["egress"]
    assert len(egress) == 1
    assert egress[0]["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "api"
    assert egress[0]["ports"] == [{"protocol": "TCP", "port": 8000}]
    ingress = _policy("api-ingress-studio")["spec"]["ingress"][0]
    assert ingress["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "studio"


def test_worker_public_egress_blocks_private_ranges_and_allows_gateway_4001():
    rules = _policy("worker-egress-litellm-and-https")["spec"]["egress"]
    gateway = rules[0]["to"][0]["ipBlock"]
    assert gateway["cidr"] == "192.168.5.2/32"
    assert rules[0]["ports"] == [{"protocol": "TCP", "port": 4001}]
    public = rules[1]["to"][0]["ipBlock"]
    assert public["cidr"] == "0.0.0.0/0"
    assert set(public["except"]) == {
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "192.168.5.2/32",
    }
    assert rules[1]["ports"] == [{"protocol": "TCP", "port": 443}]


def test_host_path_is_only_the_backup_placeholder():
    paths = []
    for doc in _workloads():
        for vol in pod_spec(doc).get("volumes") or []:
            host = vol.get("hostPath")
            if host:
                paths.append(host.get("path"))
    assert paths == ["/var/lib/pydanticos-backups"]
    assert all(not str(path).startswith("/Users") for path in paths)


def test_worker_and_api_alias_host_lima_internal():
    for kind, name in (("StatefulSet", "worker"), ("Deployment", "api")):
        docs = [d for d in kustomize_docs() if d["kind"] == kind and d["metadata"]["name"] == name]
        assert len(docs) == 1
        aliases = pod_spec(docs[0]).get("hostAliases") or []
        assert any(
            alias.get("ip") == "192.168.5.2" and "host.lima.internal" in (alias.get("hostnames") or [])
            for alias in aliases
        ), aliases


def test_backup_job_publishes_only_a_listed_dump():
    script_path = K8S_DIR / "backup-job.sh"
    script = script_path.read_text()
    maps = [
        d
        for d in kustomize_docs()
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "postgres-backup-script"
    ]
    assert len(maps) == 1
    rendered = maps[0]["data"]["backup.sh"]
    assert rendered == script
    for needle in (
        "pg_isready",
        "BACKUP_READY_TIMEOUT:-60",
        ".partial",
        "pg_restore --list",
        "trap cleanup EXIT",
        "mv --",
        "newest_valid",
        "BACKUP_RETENTION_DAYS:-30",
    ):
        assert needle in rendered, needle
    cron = [d for d in kustomize_docs() if d["kind"] == "CronJob" and d["metadata"]["name"] == "postgres-backup"]
    container = containers(cron[0])[0]
    assert container["command"] == ["sh", "-eu", "/scripts/backup.sh"]
    assert "pg_dump" not in " ".join(container["command"])
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    assert mounts["/scripts"]["name"] == "script"
    volumes = {v["name"]: v for v in pod_spec(cron[0])["volumes"]}
    assert volumes["script"]["configMap"]["name"] == "postgres-backup-script"
    assert volumes["backups"]["hostPath"]["path"] == "/var/lib/pydanticos-backups"


def test_migrate_and_backup_pods_are_labeled():
    jobs = [d for d in kustomize_docs() if d["kind"] == "Job" and d["metadata"]["name"] == "migrate"]
    assert jobs[0]["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"] == "migrate"
    cron = [d for d in kustomize_docs() if d["kind"] == "CronJob" and d["metadata"]["name"] == "postgres-backup"]
    labels = cron[0]["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "postgres-backup"


def test_secret_templates_have_no_real_values():
    template = yaml.safe_load((K8S_DIR / "secret.template.yaml").read_text())
    for mapping_key in ("data", "stringData"):
        values = template.get(mapping_key) or {}
        for key, value in values.items():
            assert value in ("", None), f"{mapping_key}.{key} is not empty"
    example = (K8S_DIR / "secrets.example.env").read_text()
    for line in example.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        _, _, raw = line.partition("=")
        assert raw.strip() == "", line


def test_allowed_tools_name_the_kb_source_tools_and_spell_tickets_correctly():
    config = next(d for d in kustomize_docs() if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "pydanticos-config")
    allowed = set(config["data"]["EAP_ALLOWED_TOOLS"].split(","))
    assert "ticket.create" not in allowed and "tickets.create" in allowed
    assert {"clock.now", "secrets.request", "http.request", "workflow.return", "feed.parse", "json.query", "text.check"} <= allowed  # general tools every tenant agent may be granted
    assert {"kb.source.get", "settings.connection.check", "kb.source.sync", "workflow.run.wait",
            "workflow.run.result"} <= allowed

def test_api_can_reach_managed_mcp_servers_like_the_worker() -> None:
    """The API's connection test and start path talk to the per-tenant proxy Services on 8080, so the API
    needs the same egress rule as the worker (deploy-host 2026-11-18: tests reported connect_failed)."""
    import yaml
    from pathlib import Path

    docs = [d for d in yaml.safe_load_all((Path(__file__).resolve().parents[2] / "deploy/k8s/networkpolicy.yaml").read_text()) if d]
    by_name = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "NetworkPolicy"}
    api = by_name["api-egress-mcp-tenants"]
    worker = by_name["worker-egress-mcp-tenants"]
    assert api["spec"]["podSelector"] == {"matchLabels": {"app.kubernetes.io/name": "api"}}
    assert api["spec"]["egress"] == worker["spec"]["egress"]


def test_up_sh_replaces_the_kube_api_port_in_block_style_yaml(tmp_path) -> None:
    """kustomize re-emits `{protocol: TCP, port: 6443}` as block-style YAML, so the substitution must match
    `port: 6443` at the end of a line (BSD sed on the runner has no \\b). deploy-host's endpoint port is 55436."""
    import re
    from pathlib import Path

    up = (Path(__file__).resolve().parents[2] / "deploy/scripts/up.sh").read_text()
    m = re.search(r'-e "s\|port: 6443\$\|port: \$\{api_endpoint_port\}\|g"', up)
    assert m, "up.sh must substitute the block-style port line"
    sample = "      ports:\n        - port: 443\n          protocol: TCP\n        - port: 6443\n          protocol: TCP\n"
    import subprocess
    out = subprocess.run(["sed", "-e", "s|port: 6443$|port: 55436|g"], input=sample, capture_output=True, text=True, check=True).stdout
    assert "port: 55436" in out and "6443" not in out
