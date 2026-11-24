"""A small async HTTP client for the OpenFGA API (v1.8+: BatchCheck). Every failure is `PolicyUnavailable`: the engine
is unreachable, slow or broken, and the caller fails closed. The error text names the call and the HTTP status (and
OpenFGA's own error code when it is a plain identifier), never a response body, a tuple or the preshared key."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx2
from eap_iam import PolicyUnavailable

from eap_iam_openfga.settings import MAX_TUPLES_PER_WRITE, OpenFgaSettings

_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
READ_PAGE_SIZE = 100
MAX_READ_PAGES = 10_000  # a runaway continuation loop ends in an error, not forever


@dataclass(frozen=True)
class TupleKey:
    user: str
    relation: str
    object: str

    def as_json(self) -> dict[str, str]:
        return {"user": self.user, "relation": self.relation, "object": self.object}


@dataclass(frozen=True)
class CheckItem:
    key: TupleKey
    contextual: tuple[TupleKey, ...] = ()


@dataclass
class CallStats:
    """Every request this client made, by name: what the batching tests assert on."""

    calls: list[str] = field(default_factory=list)

    def count(self, name: str | None = None) -> int:
        return len(self.calls) if name is None else self.calls.count(name)


class OpenFgaClient:
    def __init__(self, settings: OpenFgaSettings, http: httpx2.AsyncClient | None = None) -> None:
        self.settings = settings
        headers = {"Authorization": f"Bearer {settings.preshared_key}"} if settings.preshared_key else {}
        self._http = http or httpx2.AsyncClient(timeout=settings.timeout, headers=headers)
        # An injected client (a test transport) carries its own headers; ours are added per request in that case.
        self._headers = headers
        self.stats = CallStats()

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- transport -------------------------------------------------------------------------------------------------

    async def _call(self, name: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.stats.calls.append(name)
        call = f"{method} {name}"
        try:
            response = await self._http.request(
                method, self.settings.url + path, json=body, headers=self._headers, timeout=self.settings.timeout
            )
        except httpx2.HTTPError as exc:
            raise PolicyUnavailable(f"openfga {call} failed: {type(exc).__name__}") from None
        if response.status_code >= 400:
            code = ""
            try:
                value = response.json().get("code")
                code = f" ({value})" if isinstance(value, str) and _CODE.match(value) else ""
            except (ValueError, AttributeError):
                pass
            raise PolicyUnavailable(f"openfga {call} -> HTTP {response.status_code}{code}")
        try:
            data = response.json()
        except ValueError:
            raise PolicyUnavailable(f"openfga {call} -> an answer that is not JSON") from None
        if not isinstance(data, dict):
            raise PolicyUnavailable(f"openfga {call} -> an unexpected answer")
        return data

    def _store(self, path: str) -> str:
        return f"/stores/{quote(self.settings.store_id, safe='')}{path}"

    def _with_model(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.settings.model_id:
            body["authorization_model_id"] = self.settings.model_id
        return body

    @staticmethod
    def _contextual(tuples: Sequence[TupleKey]) -> dict[str, Any]:
        return {"contextual_tuples": {"tuple_keys": [t.as_json() for t in tuples]}} if tuples else {}

    # --- decisions -------------------------------------------------------------------------------------------------

    async def check(self, key: TupleKey, contextual: Sequence[TupleKey] = ()) -> bool:
        body = self._with_model({"tuple_key": key.as_json(), **self._contextual(contextual)})
        data = await self._call("check", "POST", self._store("/check"), body)
        allowed = data.get("allowed")
        if not isinstance(allowed, bool):
            raise PolicyUnavailable("openfga POST check -> an answer with no decision")
        return allowed

    async def batch_check(self, items: Sequence[CheckItem]) -> list[bool]:
        """One request per `batch_size` items (OpenFGA's own limit); every item is answered or the call fails."""
        answers: list[bool] = []
        size = self.settings.batch_size
        for start in range(0, len(items), size):
            chunk = items[start : start + size]
            checks = [
                {"tuple_key": item.key.as_json(), "correlation_id": str(i), **self._contextual(item.contextual)}
                for i, item in enumerate(chunk)
            ]
            data = await self._call("batch_check", "POST", self._store("/batch-check"), self._with_model({"checks": checks}))
            result = data.get("result")
            if not isinstance(result, dict):
                raise PolicyUnavailable("openfga POST batch_check -> an answer with no result")
            for i in range(len(chunk)):
                entry = result.get(str(i))
                if not isinstance(entry, dict) or not isinstance(entry.get("allowed"), bool):
                    # A per-item error (or a missing item) is an engine failure, never a quiet deny.
                    raise PolicyUnavailable("openfga POST batch_check -> an item with no decision")
                answers.append(entry["allowed"])
        return answers

    async def list_objects(self, user: str, relation: str, type_: str, contextual: Sequence[TupleKey] = ()) -> list[str]:
        body = self._with_model({"user": user, "relation": relation, "type": type_, **self._contextual(contextual)})
        data = await self._call("list_objects", "POST", self._store("/list-objects"), body)
        objects = data.get("objects")
        if not isinstance(objects, list) or not all(isinstance(o, str) for o in objects):
            raise PolicyUnavailable("openfga POST list_objects -> an answer with no objects")
        return objects

    # --- tuples ----------------------------------------------------------------------------------------------------

    async def write(self, writes: Sequence[TupleKey] = (), deletes: Sequence[TupleKey] = ()) -> None:
        """Idempotent: a tuple that already exists is not an error on write, one that is gone is not on delete.
        At most `MAX_TUPLES_PER_WRITE` tuples per request (writes first, then deletes)."""
        ops = [("w", t) for t in writes] + [("d", t) for t in deletes]
        for start in range(0, len(ops), MAX_TUPLES_PER_WRITE):
            chunk = ops[start : start + MAX_TUPLES_PER_WRITE]
            body: dict[str, Any] = {}
            w = [t.as_json() for kind, t in chunk if kind == "w"]
            d = [t.as_json() for kind, t in chunk if kind == "d"]
            if w:
                body["writes"] = {"tuple_keys": w, "on_duplicate": "ignore"}
            if d:
                body["deletes"] = {"tuple_keys": d, "on_missing": "ignore"}
            await self._call("write", "POST", self._store("/write"), self._with_model(body))

    async def read(self, key: dict[str, str] | None = None) -> list[TupleKey]:
        """Every tuple matching `key` (a partial tuple key; None: the whole store), all pages."""
        found: list[TupleKey] = []
        token = ""
        for _ in range(MAX_READ_PAGES):
            body: dict[str, Any] = {"page_size": READ_PAGE_SIZE}
            if key:
                body["tuple_key"] = key
            if token:
                body["continuation_token"] = token
            data = await self._call("read", "POST", self._store("/read"), body)
            for entry in data.get("tuples") or ():
                k = entry.get("key") if isinstance(entry, dict) else None
                if isinstance(k, dict) and all(isinstance(k.get(f), str) for f in ("user", "relation", "object")):
                    found.append(TupleKey(k["user"], k["relation"], k["object"]))
            token = data.get("continuation_token") or ""
            if not token:
                return found
        raise PolicyUnavailable("openfga POST read -> too many pages")

    # --- stores and models (bootstrap) -----------------------------------------------------------------------------

    async def list_stores(self) -> list[dict[str, Any]]:
        stores: list[dict[str, Any]] = []
        token = ""
        for _ in range(MAX_READ_PAGES):
            path = "/stores?page_size=100" + (f"&continuation_token={quote(token, safe='')}" if token else "")
            data = await self._call("list_stores", "GET", path)
            stores += [s for s in data.get("stores") or () if isinstance(s, dict)]
            token = data.get("continuation_token") or ""
            if not token:
                return stores
        raise PolicyUnavailable("openfga GET list_stores -> too many pages")

    async def create_store(self, name: str) -> str:
        data = await self._call("create_store", "POST", "/stores", {"name": name})
        store_id = data.get("id")
        if not isinstance(store_id, str) or not store_id:
            raise PolicyUnavailable("openfga POST create_store -> an answer with no id")
        return store_id

    async def latest_model(self) -> dict[str, Any] | None:
        data = await self._call("read_models", "GET", self._store("/authorization-models?page_size=1"))
        models = data.get("authorization_models") or []
        return models[0] if models and isinstance(models[0], dict) else None

    async def write_model(self, model: dict[str, Any]) -> str:
        data = await self._call("write_model", "POST", self._store("/authorization-models"), model)
        model_id = data.get("authorization_model_id")
        if not isinstance(model_id, str) or not model_id:
            raise PolicyUnavailable("openfga POST write_model -> an answer with no id")
        return model_id
