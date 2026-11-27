"""A stateful fake OpenFGA behind `httpx2.MockTransport`: just the calls the plug-in makes, with OpenFGA's field names.

It does not evaluate a model: a decision is a membership test on `allowed` (a set of (user, relation, object)); the
model's semantics are covered by `fga model test` and by the integration test against a real server. It counts every
call by endpoint, which is what the batching tests assert on."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

import httpx2

from eap_iam_openfga.client import OpenFgaClient
from eap_iam_openfga.settings import OpenFgaSettings

# What a real server (v1.23.0) accepts: `type:id` with exactly one ':', no whitespace, control character or '#' in the id,
# optionally `#relation` on a user; an object of at most 256 characters, a user of at most 512 bytes.
_ID = r"[a-z_]+:[^:#\s\x00-\x1f\x7f-\x9f]+"
_OBJECT = re.compile(rf"^{_ID}$")
_USER = re.compile(rf"^{_ID}(#[a-z_]+)?$")


def malformed(body: dict[str, Any]) -> bool:
    """Whether a real server would answer this request 400 `validation_error` for one of its ids."""
    keys = [body.get("tuple_key")] + [c.get("tuple_key") for c in body.get("checks") or ()]
    for holder in (body, *(body.get("checks") or ())):
        keys += (holder.get("contextual_tuples") or {}).get("tuple_keys") or []
    for part in ("writes", "deletes"):
        keys += (body.get(part) or {}).get("tuple_keys") or []
    keys.append({"user": body.get("user")})
    for key in keys:
        user, obj = (key or {}).get("user"), (key or {}).get("object")
        if user and (not _USER.match(user) or len(user.encode()) > 512):
            return True
        # Read takes `type:` to mean every object of a type.
        if obj and not obj.endswith(":") and (not _OBJECT.match(obj) or len(obj) > 256):
            return True
    return False


KEY = "fake-preshared-key-0123456789"
URL = "http://openfga.test:8080"
STORE = "01FAKESTORE"


def settings(**over: Any) -> OpenFgaSettings:
    base = dict(url=URL, store_id=STORE, model_id="01FAKEMODEL", preshared_key=KEY)
    base.update(over)
    return OpenFgaSettings(**base)


class FakeOpenFga:
    def __init__(self) -> None:
        self.allowed: set[tuple[str, str, str]] = set()
        self.tuples: set[tuple[str, str, str]] = set()
        self.stores: list[dict[str, Any]] = []
        self.models: list[dict[str, Any]] = []
        self.calls: Counter[str] = Counter()
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.fail: Callable[[str], httpx2.Response | Exception | None] | None = None
        self.contextual_seen: list[list[dict[str, str]]] = []
        self.list_cap: int | None = None  # a server-side result limit
        self.auth_ok = True

    # --- plumbing --------------------------------------------------------------------------------------------------

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def client(self, **over: Any) -> OpenFgaClient:
        return OpenFgaClient(settings(**over), httpx2.AsyncClient(transport=self.transport()))

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        name = self._name(request.method, path)
        self.calls[name] += 1
        body = json.loads(request.content) if request.content else {}
        self.requests.append((name, body))
        if request.headers.get("authorization") != f"Bearer {KEY}":
            self.auth_ok = False
        if self.fail is not None and (outcome := self.fail(name)) is not None:
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if malformed(body):
            return httpx2.Response(400, json={"code": "validation_error"})
        handler = getattr(self, f"_{name}")
        return httpx2.Response(200, json=handler(request, body))

    @staticmethod
    def _name(method: str, path: str) -> str:
        parts = path.strip("/").split("/")
        if parts == ["stores"]:
            return "create_store" if method == "POST" else "list_stores"
        tail = parts[2]
        return {
            "check": "check", "batch-check": "batch_check", "list-objects": "list_objects", "write": "write",
            "read": "read", "authorization-models": "write_model" if method == "POST" else "read_models",
        }[tail]

    # --- endpoints -------------------------------------------------------------------------------------------------

    def _allowed(self, key: dict[str, str], contextual: dict[str, Any] | None) -> bool:
        keys = (contextual or {}).get("tuple_keys") or []
        self.contextual_seen.append(keys)
        triple = (key["user"], key["relation"], key["object"])
        return triple in self.allowed

    def _check(self, request: httpx2.Request, body: dict[str, Any]) -> dict[str, Any]:
        return {"allowed": self._allowed(body["tuple_key"], body.get("contextual_tuples"))}

    def _batch_check(self, request: httpx2.Request, body: dict[str, Any]) -> dict[str, Any]:
        assert len(body["checks"]) <= 50, "BatchCheck is limited to 50 checks"
        return {"result": {c["correlation_id"]: {"allowed": self._allowed(c["tuple_key"], c.get("contextual_tuples"))} for c in body["checks"]}}

    def _list_objects(self, request: httpx2.Request, body: dict[str, Any]) -> dict[str, Any]:
        self.contextual_seen.append((body.get("contextual_tuples") or {}).get("tuple_keys") or [])
        found = sorted(o for u, r, o in self.allowed if u == body["user"] and r == body["relation"] and o.startswith(body["type"] + ":"))
        if self.list_cap is not None:
            found = found[: self.list_cap]
        return {"objects": found}

    def _write(self, request: httpx2.Request, body: dict[str, Any]) -> dict[str, Any]:
        writes = (body.get("writes") or {}).get("tuple_keys") or []
        deletes = (body.get("deletes") or {}).get("tuple_keys") or []
        assert len(writes) + len(deletes) <= 100, "a write is limited to 100 tuples"
        for t in writes:
            self.tuples.add((t["user"], t["relation"], t["object"]))
        for t in deletes:
            self.tuples.discard((t["user"], t["relation"], t["object"]))
        return {}

    def _read(self, request: httpx2.Request, body: dict[str, Any]) -> dict[str, Any]:
        key = body.get("tuple_key") or {}
        found = sorted(
            t for t in self.tuples
            if all(
                (not key.get(f)) or (t[i] == key[f] or (f == "object" and key[f].endswith(":") and t[2].startswith(key[f])))
                for i, f in enumerate(("user", "relation", "object"))
            )
        )
        size = int(body.get("page_size") or 100)
        start = int(body.get("continuation_token") or 0)
        page = found[start : start + size]
        nxt = str(start + size) if start + size < len(found) else ""
        return {"tuples": [{"key": {"user": u, "relation": r, "object": o}} for u, r, o in page], "continuation_token": nxt}

    def _list_stores(self, request: httpx2.Request, body: dict[str, Any]) -> dict[str, Any]:
        return {"stores": self.stores, "continuation_token": ""}

    def _create_store(self, request: httpx2.Request, body: dict[str, Any]) -> dict[str, Any]:
        store = {"id": f"01STORE{len(self.stores)}", "name": body["name"]}
        self.stores.append(store)
        return store

    def _read_models(self, request: httpx2.Request, body: dict[str, Any]) -> dict[str, Any]:
        return {"authorization_models": self.models[-1:][::-1]}

    def _write_model(self, request: httpx2.Request, body: dict[str, Any]) -> dict[str, Any]:
        model_id = f"01MODEL{len(self.models)}"
        # The server stores a model with defaults filled in (this is what a real one returns).
        stored = json.loads(json.dumps(body))
        for t in stored["type_definitions"]:
            for meta in ((t.get("metadata") or {}).get("relations") or {}).values():
                meta.setdefault("module", "")
                meta.setdefault("source_info", None)
                for d in meta.get("directly_related_user_types") or []:
                    d.setdefault("condition", "")
        self.models.append({"id": model_id, **stored})
        return {"authorization_model_id": model_id}
