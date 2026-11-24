"""The OpenFGA policy engine (design 5.4 to 5.6).

Decisions: the principal's tenant roles go with every request as contextual tuples (`user:<subject> admin tenant:<T>`),
so a role change needs no sync; resource links and per-resource grants are stored tuples. Cost: `check` is one call,
`check_many` one BatchCheck (or, above OpenFGA's batch limit, one ListObjects), `filter_ids` one Check (is the caller a
tenant admin) plus one ListObjects. Nothing is cached across requests (a revoked right stops at once).

Fail closed: any engine failure raises `PolicyUnavailable`; the entry point answers 503. A request the engine cannot even
express (an unknown action, a resource of another tenant) is denied without a call."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from eap_iam import (
    All,
    Decision,
    Grant,
    GRANT_RELATIONS,
    GrantError,
    IdFilter,
    Only,
    Principal,
    ResourceRef,
)

from eap_iam_openfga.client import CheckItem, OpenFgaClient, TupleKey
from eap_iam_openfga.names import (
    KIND_RUN,
    KIND_TENANT,
    ROLE_RELATIONS,
    grant_relation_ok,
    object_ref,
    relation_for,
    strip_object,
    tenant_ref,
    user_ref,
    valid_id,
)

log = logging.getLogger("eap.iam.openfga")

FEATURES = frozenset({"grants", "list_filter", "explain", "contextual_roles"})


def _deny(reason: str, **explain: str) -> Decision:
    return Decision(allowed=False, reason=reason, explain=dict(explain))


class OpenFgaPolicy:
    name = "openfga"
    transitional = False
    features = FEATURES

    def __init__(self, client: OpenFgaClient) -> None:
        self.client = client

    # --- who is asking ---------------------------------------------------------------------------------------------

    @staticmethod
    def _subject(principal: Principal) -> str | None:
        """The OpenFGA user a decision is for: the principal itself, or for a run the principal it acts for."""
        subject = principal.on_behalf_of if principal.kind == "run" else principal.subject
        return subject if subject and valid_id(subject) else None

    @staticmethod
    def _abstains(principal: Principal) -> bool:
        # An API key is bounded by the scopes and allowlists it carries (eap_iam.permits, evaluated before the engine);
        # keys are never members, so there is nothing to ask OpenFGA (design 2.3).
        return principal.kind == "api_key"

    @staticmethod
    def _contextual(principal: Principal, user: str) -> tuple[TupleKey, ...]:
        tenant = tenant_ref(principal.tenant_id)
        return tuple(TupleKey(user, role, tenant) for role in ROLE_RELATIONS if role in principal.roles)

    # --- decisions -------------------------------------------------------------------------------------------------

    def _item(self, principal: Principal, action: str, resource: ResourceRef) -> tuple[CheckItem | None, Decision | None]:
        """The check to make, or the decision that needs no call."""
        if resource.tenant_id != principal.tenant_id:
            return None, _deny("deny:cross_tenant")
        subject = self._subject(principal)
        if subject is None:
            return None, _deny("deny:no_subject")
        relation = relation_for(action, resource.kind, resource.id is not None)
        if relation is None:
            return None, _deny("deny:unknown_action", action=action, kind=resource.kind)
        try:
            if resource.kind == KIND_TENANT or resource.id is None:
                obj = tenant_ref(resource.tenant_id)
            else:
                obj = object_ref(resource)
        except GrantError:
            return None, _deny("deny:invalid_resource", kind=resource.kind)
        user = user_ref(subject)
        return CheckItem(TupleKey(user, relation, obj), self._contextual(principal, user)), None

    @staticmethod
    def _decision(allowed: bool, item: CheckItem) -> Decision:
        relation = item.key.relation
        reason = f"allow:{relation}" if allowed else "deny:no_grant"
        return Decision(allowed=allowed, reason=reason, explain={"relation": relation})

    async def check(self, principal: Principal, action: str, resource: ResourceRef) -> Decision:
        if self._abstains(principal):
            return Decision(allowed=True, reason="abstain:constraints", explain={"rule": "openfga"})
        item, decision = self._item(principal, action, resource)
        if item is None:
            assert decision is not None
            return decision
        return self._decision(await self.client.check(item.key, item.contextual), item)

    async def check_many(
        self, principal: Principal, action: str, resources: Sequence[ResourceRef]
    ) -> dict[str, Decision]:
        out: dict[str, Decision] = {}
        pending: list[tuple[str, CheckItem]] = []
        for resource in resources:
            key = str(resource.id)
            if self._abstains(principal):
                out[key] = Decision(allowed=True, reason="abstain:constraints", explain={"rule": "openfga"})
                continue
            item, decision = self._item(principal, action, resource)
            if item is None:
                assert decision is not None
                out[key] = decision
            elif key not in {k for k, _ in pending}:
                pending.append((key, item))
        if not pending:
            return out
        size = self.client.settings.batch_size
        kinds = {item.key.object.partition(":")[0] for _, item in pending}
        relations = {item.key.relation for _, item in pending}
        if len(pending) > size and len(kinds) == 1 and len(relations) == 1:
            # More than one BatchCheck would take: one ListObjects answers them all (a constant cost). A truncated
            # list falls back to the batches below, so a long list is never read as "not allowed".
            answers = await self._answer_by_listing(principal, pending)
        else:
            answers = None
        if answers is None:
            answers = dict(zip((k for k, _ in pending), await self.client.batch_check([i for _, i in pending]), strict=True))
        for key, item in pending:
            out[key] = self._decision(answers[key], item)
        return out

    async def _answer_by_listing(
        self, principal: Principal, pending: list[tuple[str, CheckItem]]
    ) -> dict[str, bool] | None:
        first = pending[0][1]
        kind = first.key.object.partition(":")[0]
        objects = await self.client.list_objects(first.key.user, first.key.relation, kind, first.contextual)
        if len(objects) >= self.client.settings.list_limit:
            log.warning("openfga list_objects reached its limit (%s); falling back to batch checks", self.client.settings.list_limit)
            return None
        allowed = set(objects)
        return {key: item.key.object in allowed for key, item in pending}

    async def filter_ids(
        self, principal: Principal, action: str, kind: str, candidate_ids: Sequence[str] | None = None
    ) -> IdFilter:
        if self._abstains(principal):
            return All()
        subject = self._subject(principal)
        relation = relation_for(action, kind, True)
        if subject is None or relation is None or kind in (KIND_TENANT,):
            return Only(())
        user = user_ref(subject)
        contextual = self._contextual(principal, user)
        # A tenant admin holds every action on every resource of the tenant: one Check, no list.
        if await self.client.check(TupleKey(user, "admin", tenant_ref(principal.tenant_id)), contextual):
            return All()
        objects = await self.client.list_objects(user, relation, kind, contextual)
        ids = [i for o in objects if (i := strip_object(o, kind, principal.tenant_id)) is not None]
        candidates = None if candidate_ids is None else list(dict.fromkeys(candidate_ids))
        if len(objects) < self.client.settings.list_limit:
            if candidates is None:
                return Only(tuple(ids))
            wanted = set(ids)
            return Only(tuple(c for c in candidates if c in wanted))
        log.warning("openfga list_objects reached its limit (%s) for kind %s", self.client.settings.list_limit, kind)
        if candidates is None:
            # No page to check: the answer is the part the engine returned, which can only hide rows, never show one.
            return Only(tuple(ids))
        items = []
        for c in candidates:
            if not valid_id(c):
                continue
            items.append((c, CheckItem(TupleKey(user, relation, f"{kind}:{principal.tenant_id}/{c}"), contextual)))
        answers = await self.client.batch_check([i for _, i in items])
        return Only(tuple(c for (c, _), ok in zip(items, answers, strict=True) if ok))

    # --- resources and grants --------------------------------------------------------------------------------------

    async def register_resource(
        self, resource: ResourceRef, creator: str | None = None, parent: ResourceRef | None = None
    ) -> None:
        """Link a new resource to its tenant and make `creator` its owner (a run: link it to its parent). Idempotent."""
        obj = object_ref(resource)
        tenant = tenant_ref(resource.tenant_id)
        writes: list[TupleKey] = []
        if resource.kind == KIND_RUN:
            if parent is None or parent.kind not in ("agent", "workflow") or parent.tenant_id != resource.tenant_id:
                raise GrantError("invalid_resource", "a run is registered with its agent or workflow")
            writes.append(TupleKey(object_ref(parent), "parent", obj))
        elif resource.kind == KIND_TENANT:
            raise GrantError("invalid_resource", "a tenant is not registered")
        else:
            writes.append(TupleKey(tenant, "tenant", obj))
            if creator:
                writes.append(TupleKey(user_ref(creator), "owner", obj))
        await self.client.write(writes)

    async def unregister_resource(self, resource: ResourceRef) -> None:
        """Delete every tuple of the resource (links and grants), and for an agent or workflow the run links below it."""
        obj = object_ref(resource)
        found = await self.client.read({"object": obj})
        if resource.kind in ("agent", "workflow"):
            found += await self.client.read({"user": obj, "object": f"{KIND_RUN}:"})
        if found:
            await self.client.write(deletes=found)

    def _grant_key(self, grant: Grant) -> TupleKey:
        if not grant_relation_ok(grant.resource.kind, grant.relation):
            raise GrantError(
                "invalid_relation", f"{grant.relation!r} cannot be granted on a {grant.resource.kind} (allowed: {', '.join(GRANT_RELATIONS)})"
            )
        return TupleKey(user_ref(grant.subject), grant.relation, object_ref(grant.resource))

    async def grant(self, grant: Grant) -> None:
        await self.client.write([self._grant_key(grant)])

    async def revoke(self, grant: Grant) -> None:
        await self.client.write(deletes=[self._grant_key(grant)])

    async def list_grants(self, resource: ResourceRef) -> list[Grant]:
        obj = object_ref(resource)
        tuples = await self.client.read({"object": obj})
        grants = [
            Grant(t.user.removeprefix("user:"), t.relation, resource)
            for t in tuples
            if t.relation in GRANT_RELATIONS and t.user.startswith("user:") and not t.user.startswith("user:*")
        ]
        return sorted(grants, key=lambda g: (g.relation, g.subject))
