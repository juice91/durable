"""Locks for the kube-router programming race and the Loki query path.

These assert the behavior a fresh rollout needs. They fail while the manifests still
connect once from a brand-new pod, and while no policy allows the API to reach Loki.
"""

from __future__ import annotations

from pathlib import Path

from kustomize_util import kustomize_docs

ROOT = Path(__file__).resolve().parents[2]


def _policy_ports(rule: dict) -> set[tuple[str | None, object]]:
    return {(item.get("protocol"), item.get("port")) for item in rule.get("ports") or []}


def test_migrate_survives_a_refused_first_connection_in_the_same_pod():
    """A new pod can be rejected until kube-router programs its NetworkPolicy.

    restartPolicy Never starts the next attempt in a new pod, so every attempt can lose
    the same race. An in-container retry, or OnFailure/Always in the same pod, survives it.
    """
    entrypoint = (ROOT / "deploy" / "docker" / "python-entrypoint.sh").read_text()
    migrate_branch = entrypoint.split("migrate)", 1)[1].split(";;", 1)[0]
    retries_in_container = any(token in migrate_branch for token in ("pg_isready", "until ", "while "))
    manifest = (ROOT / "deploy" / "k8s" / "migrate.yaml").read_text()
    same_pod = "restartPolicy: OnFailure" in manifest or "restartPolicy: Always" in manifest
    if not (retries_in_container or same_pod):
        raise AssertionError(
            "migrate.yaml uses restartPolicy Never and the entrypoint runs alembic once; "
            "a refused first connection fails that pod, and the next attempt is a new pod"
        )


def test_api_can_reach_loki_on_port_3100():
    """Studio log search calls the API, and the API is configured with EAP_LOKI_URL :3100."""
    egress = False
    ingress = False
    for policy in kustomize_docs():
        if policy.get("kind") != "NetworkPolicy":
            continue
        spec = policy.get("spec") or {}
        selected = ((spec.get("podSelector") or {}).get("matchLabels") or {}).get(
            "app.kubernetes.io/name"
        )
        for rule in spec.get("egress") or []:
            if ("TCP", 3100) not in _policy_ports(rule):
                continue
            for peer in rule.get("to") or []:
                name = ((peer.get("podSelector") or {}).get("matchLabels") or {}).get(
                    "app.kubernetes.io/name"
                )
                if name == "lgtm" and selected in (None, "api"):
                    egress = True
        if selected == "lgtm":
            for rule in spec.get("ingress") or []:
                if ("TCP", 3100) in _policy_ports(rule):
                    ingress = True
    if not (egress and ingress):
        raise AssertionError(
            "no NetworkPolicy allows the API to reach lgtm TCP 3100, "
            "but the config sets EAP_LOKI_URL to http://lgtm:3100"
        )
