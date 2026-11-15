"""Load `kubectl kustomize deploy/k8s` as YAML documents."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from typing import Any

from pathlib import Path

import yaml

K8S_DIR = Path(__file__).resolve().parents[2] / "deploy" / "k8s"

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "Job", "CronJob"}
LONG_RUNNING = {"Deployment", "StatefulSet"}


@lru_cache(maxsize=1)
def kustomize_docs() -> tuple[dict[str, Any], ...]:
    proc = subprocess.run(
        ["kubectl", "kustomize", str(K8S_DIR)],
        check=True,
        capture_output=True,
        text=True,
    )
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    return tuple(docs)


def pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    kind = doc["kind"]
    spec = doc["spec"]
    if kind == "CronJob":
        return spec["jobTemplate"]["spec"]["template"]["spec"]
    if kind in {"Job", "Deployment", "StatefulSet"}:
        return spec["template"]["spec"]
    raise KeyError(kind)


def containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    spec = pod_spec(doc)
    return list(spec.get("containers") or []) + list(spec.get("initContainers") or [])
