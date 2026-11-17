"""An in-memory `KubeApi` standing in for the Kubernetes API server."""

from __future__ import annotations

import copy
from typing import Any


class FakeKube:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str | None, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, str, str | None, str]] = []
        self.pods: dict[str, list[str]] = {}
        self.logs: dict[str, str] = {}
        self.pod_status: dict[str, list[dict[str, Any]]] = {}  # namespace -> pod dicts

    def _key(self, doc: dict[str, Any]) -> tuple[str, str | None, str]:
        md = doc["metadata"]
        return doc["kind"], md.get("namespace"), md["name"]

    async def apply(self, doc: dict[str, Any]) -> None:
        k = self._key(doc)
        self.calls.append(("apply", *k))
        self.objects[k] = copy.deepcopy(doc)

    async def get(self, kind: str, namespace: str | None, name: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.objects.get((kind, namespace, name)))

    async def delete(self, kind: str, namespace: str | None, name: str) -> None:
        self.calls.append(("delete", kind, namespace, name))
        self.objects.pop((kind, namespace, name), None)

    async def merge_patch(self, kind: str, namespace: str, name: str, body: dict[str, Any]) -> None:
        self.calls.append(("patch", kind, namespace, name))
        obj = self.objects[(kind, namespace, name)]
        for section, values in body.items():
            obj.setdefault(section, {}).update(values)

    async def pod_names(self, namespace: str, label_selector: str) -> list[str]:
        return self.pods.get(namespace, [])

    async def pod_log(self, namespace: str, pod: str, tail: int) -> str:
        return "\n".join(self.logs.get(pod, "").splitlines()[-tail:])

    async def pod_list(self, namespace: str, label_selector: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.pod_status.get(namespace, []))
