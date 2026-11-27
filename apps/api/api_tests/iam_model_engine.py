"""Test doubles for per-resource authorisation (IAM P2b).

`ModelServer` is an OpenFGA that EVALUATES the shipped model (`plugins/iam-openfga/model/durable.fga`) over the tuples
written to it and the contextual tuples a request sends, behind `httpx2.MockTransport`: the real `OpenFgaPolicy` and
`OpenFgaClient` run unchanged against it, so the role matrix, grants and parent links are the real model's, with no
server or Docker. It implements what the engine calls: check, batch-check, list-objects, write and read, and counts
calls by endpoint.

`DenyAll` refuses everything (and lists nothing); `Recording` wraps an engine and counts its calls by method. Both are
`PolicyEngine`s the API accepts."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

import httpx2
from eap_iam import All, Decision, IdFilter, Only, Principal, ResourceRef
from eap_iam_openfga.client import OpenFgaClient
from eap_iam_openfga.dsl import parse_model
from eap_iam_openfga.engine import OpenFgaPolicy
from eap_iam_openfga.model import model_text
from eap_iam_openfga.settings import OpenFgaSettings

Tuple3 = tuple[str, str, str]

# The ids a real server accepts: `type:id` with one ':' only (and `#relation` on a userset).
_ID = re.compile(r"^[a-z_]+:[^:#\s]+(#[a-z_]+)?$")


def malformed(body: dict[str, Any]) -> bool:
    keys = [body.get("tuple_key"), {"user": body.get("user")}]
    for holder in (body, *(body.get("checks") or ())):
        keys += [holder.get("tuple_key"), *((holder.get("contextual_tuples") or {}).get("tuple_keys") or [])]
    for part in ("writes", "deletes"):
        keys += (body.get(part) or {}).get("tuple_keys") or []
    values = [v for k in keys if k for v in (k.get("user"), k.get("object")) if v]
    return any(not v.endswith(":") and not _ID.match(v) for v in values)


class ModelServer:
    def __init__(self) -> None:
        self.types = {t["type"]: t.get("relations", {}) for t in parse_model(model_text())["type_definitions"]}
        self.tuples: set[Tuple3] = set()
        self.calls: Counter[str] = Counter()

    # --- the model -------------------------------------------------------------------------------------------------

    def _eval(self, user: str, relation: str, obj: str, tuples: set[Tuple3], depth: int = 0) -> bool:
        if depth > 12:
            return False
        kind = obj.partition(":")[0]
        rule = self.types.get(kind, {}).get(relation)
        return rule is not None and self._rule(user, rule, relation, obj, tuples, depth)

    def _rule(self, user: str, rule: dict[str, Any], relation: str, obj: str, tuples: set[Tuple3], depth: int) -> bool:
        if "this" in rule:
            if (user, relation, obj) in tuples:
                return True
            # A userset tuple (`tenant:T#viewer runner workflow:T/1`): everyone who holds that relation on that object.
            return any(
                self._eval(user, u.partition("#")[2], u.partition("#")[0], tuples, depth + 1)
                for u, r, o in list(tuples) if r == relation and o == obj and "#" in u
            )
        if "computedUserset" in rule:
            return self._eval(user, rule["computedUserset"]["relation"], obj, tuples, depth + 1)
        if "tupleToUserset" in rule:
            via = rule["tupleToUserset"]["tupleset"]["relation"]
            target = rule["tupleToUserset"]["computedUserset"]["relation"]
            # A tuple (parent, via, obj) names the parent object as its "user" (`tenant:T tenant agent:T/1`).
            return any(
                self._eval(user, target, parent, tuples, depth + 1) for parent, rel, o in list(tuples) if o == obj and rel == via
            )
        if "union" in rule:
            return any(self._rule(user, child, relation, obj, tuples, depth) for child in rule["union"]["child"])
        raise AssertionError(f"unsupported rule {rule!r}")

    def _all(self, body: dict[str, Any]) -> set[Tuple3]:
        context = (body.get("contextual_tuples") or {}).get("tuple_keys") or []
        return self.tuples | {(t["user"], t["relation"], t["object"]) for t in context}

    # --- endpoints -------------------------------------------------------------------------------------------------

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        name = request.url.path.strip("/").split("/")[-1]
        self.calls[name] += 1
        body = json.loads(request.content) if request.content else {}
        if malformed(body):  # the ids a real server refuses
            return httpx2.Response(400, json={"code": "validation_error"})
        if name == "check":
            key = body["tuple_key"]
            return httpx2.Response(200, json={"allowed": self._eval(key["user"], key["relation"], key["object"], self._all(body))})
        if name == "batch-check":
            tuples = {}
            result = {}
            for c in body["checks"]:
                k = c["tuple_key"]
                tuples = self._all(c)
                result[c["correlation_id"]] = {"allowed": self._eval(k["user"], k["relation"], k["object"], tuples)}
            return httpx2.Response(200, json={"result": result})
        if name == "list-objects":
            tuples = self._all(body)
            candidates = {o for u, r, o in tuples if o.startswith(body["type"] + ":")} | {
                u for u, r, o in tuples if u.startswith(body["type"] + ":")
            }
            found = sorted(o for o in candidates if self._eval(body["user"], body["relation"], o, tuples))
            return httpx2.Response(200, json={"objects": found})
        if name == "write":
            for t in (body.get("writes") or {}).get("tuple_keys") or []:
                self.tuples.add((t["user"], t["relation"], t["object"]))
            for t in (body.get("deletes") or {}).get("tuple_keys") or []:
                self.tuples.discard((t["user"], t["relation"], t["object"]))
            return httpx2.Response(200, json={})
        if name == "read":
            key = body.get("tuple_key") or {}
            found = sorted(
                t for t in self.tuples
                if all(not key.get(f) or t[i] == key[f] for i, f in enumerate(("user", "relation", "object")))
            )
            return httpx2.Response(200, json={
                "tuples": [{"key": {"user": u, "relation": r, "object": o}} for u, r, o in found], "continuation_token": "",
            })
        raise AssertionError(f"unexpected OpenFGA call {name}")

    def policy(self) -> "RolesFromTable":
        settings = OpenFgaSettings(url="http://openfga.test", store_id="01TEST", model_id="01MODEL", preshared_key="k" * 24)
        client = OpenFgaClient(settings, httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle)))
        return RolesFromTable(OpenFgaPolicy(client))


class RolesFromTable:
    """The real `OpenFgaPolicy`, with the tenant roles the identity chain would put on the principal (ZITADEL's role
    claim): a table of subject -> roles, applied before every call. Everything else is the plug-in's own behaviour."""

    def __init__(self, engine: OpenFgaPolicy) -> None:
        self.engine = engine
        self.roles: dict[str, frozenset[str]] = {}
        self.name, self.transitional, self.features = engine.name, engine.transitional, engine.features

    def _with_roles(self, principal: Principal) -> Principal:
        # A run acts for a person (design 4.1): its roles are theirs.
        roles = self.roles.get(principal.on_behalf_of if principal.kind == "run" and principal.on_behalf_of else principal.subject)
        return principal.model_copy(update={"roles": roles}) if roles is not None else principal

    async def check(self, principal: Principal, action: str, resource: ResourceRef) -> Decision:
        return await self.engine.check(self._with_roles(principal), action, resource)

    async def check_many(self, principal: Principal, action: str, resources: Sequence[ResourceRef]) -> dict[str, Decision]:
        return await self.engine.check_many(self._with_roles(principal), action, resources)

    async def filter_ids(
        self, principal: Principal, action: str, kind: str, candidate_ids: Sequence[str] | None = None
    ) -> IdFilter:
        return await self.engine.filter_ids(self._with_roles(principal), action, kind, candidate_ids)

    async def register_resource(self, resource: ResourceRef, creator: str | None = None, parent: ResourceRef | None = None) -> None:
        await self.engine.register_resource(resource, creator, parent)

    async def unregister_resource(self, resource: ResourceRef) -> None:
        await self.engine.unregister_resource(resource)

    async def grant(self, grant: Any) -> None:
        await self.engine.grant(grant)

    async def revoke(self, grant: Any) -> None:
        await self.engine.revoke(grant)

    async def list_grants(self, resource: ResourceRef) -> list[Any]:
        return await self.engine.list_grants(resource)

    def grantable(self, kind: str) -> list[Any]:
        return self.engine.grantable(kind)

    def requirements(self, action: str, kind: str, has_id: bool) -> Any:
        return self.engine.requirements(action, kind, has_id)

    async def grant_use(self, grant: Any) -> None:
        await self.engine.grant_use(grant)

    async def revoke_use(self, grant: Any) -> None:
        await self.engine.revoke_use(grant)

    async def used_by(self, subject: ResourceRef, kind: str) -> list[str]:
        return await self.engine.used_by(subject, kind)

    async def grantees(self, resource: ResourceRef) -> list[ResourceRef]:
        return await self.engine.grantees(resource)


class DenyAll:
    """An engine that refuses everything and lists nothing; it records the resources it was asked to register."""

    name, transitional, features = "deny-all", False, frozenset()

    def __init__(self) -> None:
        self.registered: list[ResourceRef] = []

    async def check(self, principal: Principal, action: str, resource: ResourceRef) -> Decision:
        return Decision(allowed=False, reason="deny:test")

    async def check_many(self, principal: Principal, action: str, resources: Sequence[ResourceRef]) -> dict[str, Decision]:
        return {str(r.id): await self.check(principal, action, r) for r in resources}

    async def filter_ids(
        self, principal: Principal, action: str, kind: str, candidate_ids: Sequence[str] | None = None
    ) -> IdFilter:
        return Only(())

    async def register_resource(self, resource: ResourceRef, creator: str | None = None, parent: ResourceRef | None = None) -> None:
        self.registered.append(resource)

    async def unregister_resource(self, resource: ResourceRef) -> None:
        return None


class AllowAll(DenyAll):
    name = "allow-all"

    async def check(self, principal: Principal, action: str, resource: ResourceRef) -> Decision:
        return Decision(allowed=True, reason="allow:test")

    async def filter_ids(
        self, principal: Principal, action: str, kind: str, candidate_ids: Sequence[str] | None = None
    ) -> IdFilter:
        return All()


class Recording:
    """Counts the calls an engine receives, by method."""

    def __init__(self, engine: Any) -> None:
        self.engine, self.calls = engine, Counter()
        self.name, self.transitional, self.features = engine.name, engine.transitional, engine.features

    def __getattr__(self, attr: str) -> Any:
        target = getattr(self.engine, attr)
        if not callable(target):
            return target

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls[attr] += 1
            return await target(*args, **kwargs)

        return call
