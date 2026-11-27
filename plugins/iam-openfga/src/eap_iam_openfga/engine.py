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
    AccessRequirement,
    Decision,
    Grant,
    GRANT_RELATIONS,
    GRANT_SUBJECT_ROLE,
    GRANT_SUBJECT_USER,
    GrantableRelation,
    GrantError,
    MEMBER_ROLES,
    IdFilter,
    Only,
    Principal,
    ResourceRef,
    UseGrant,
    USE_RESOURCE_KINDS,
    USE_SUBJECT_KINDS,
)

from eap_iam_openfga.client import CheckItem, OpenFgaClient, TupleKey
from eap_iam_openfga.names import (
    KIND_RUN,
    KIND_TENANT,
    ROLE_RELATIONS,
    split_object,
    grant_relation_ok,
    grantable_relations,
    object_ref,
    relation_for,
    requirements,
    strip_object,
    subject_object_ref,
    subject_of,
    tenant_ref,
    user_ref,
)

log = logging.getLogger("eap.iam.openfga")

FEATURES = frozenset({"grants", "list_filter", "explain", "contextual_roles", "agent_grants"})
GRANTED_TO = "granted_to"  # agent-bound grants: `agent:<t>/<id> granted_to secret:<t>/<name>`


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
    def _user(principal: Principal) -> str | None:
        """The OpenFGA user a decision is for: the principal itself, or for a run the principal it acts for. None when
        there is none, or OpenFGA cannot hold it."""
        subject = principal.on_behalf_of if principal.kind == "run" else principal.subject
        try:
            return None if subject is None else user_ref(subject)
        except GrantError:
            return None

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
        user = self._user(principal)
        if user is None:
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
        user = self._user(principal)
        relation = relation_for(action, kind, True)
        if user is None or relation is None or kind in (KIND_TENANT,):
            return Only(())
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
            try:
                obj = object_ref(ResourceRef(kind, c, principal.tenant_id))
            except GrantError:
                continue
            items.append((c, CheckItem(TupleKey(user, relation, obj), contextual)))
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
            writes.append(TupleKey(subject_object_ref(parent), "parent", obj))
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
            # What this agent or workflow was granted to use goes with it.
            for kind in USE_RESOURCE_KINDS:
                found += await self.client.read({"user": obj, "object": f"{kind}:"})
        if found:
            await self.client.write(deletes=found)

    def grantable(self, kind: str) -> list[GrantableRelation]:
        return grantable_relations(kind)

    def requirements(self, action: str, kind: str, has_id: bool) -> AccessRequirement | None:
        return requirements(action, kind, has_id)

    def _grant_key(self, grant: Grant) -> TupleKey:
        if grant.subject_type not in (GRANT_SUBJECT_USER, GRANT_SUBJECT_ROLE):
            raise GrantError("invalid_subject", "a grant names a user or a role")
        role = grant.subject_type == GRANT_SUBJECT_ROLE
        if not grant_relation_ok(grant.resource.kind, grant.relation, role):
            allowed = ", ".join(g.relation for g in grantable_relations(grant.resource.kind) if g.roles or not role)
            raise GrantError(
                "invalid_relation",
                f"{grant.relation!r} cannot be granted to a {grant.subject_type} on a {grant.resource.kind} (allowed: {allowed or 'none'})",
            )
        obj = object_ref(grant.resource)
        if not role:
            return TupleKey(user_ref(grant.subject), grant.relation, obj)
        if grant.subject not in MEMBER_ROLES:
            raise GrantError("invalid_subject", f"a role is one of {', '.join(MEMBER_ROLES)}")
        # Everyone holding the role in the resource's own tenant (the role arrives as a contextual tuple per request).
        return TupleKey(f"{tenant_ref(grant.resource.tenant_id)}#{grant.subject}", grant.relation, obj)

    async def grant(self, grant: Grant) -> None:
        await self.client.write([self._grant_key(grant)])

    async def revoke(self, grant: Grant) -> None:
        await self.client.write(deletes=[self._grant_key(grant)])

    async def list_grants(self, resource: ResourceRef) -> list[Grant]:
        obj = object_ref(resource)
        tuples = await self.client.read({"object": obj})
        roles = {f"{tenant_ref(resource.tenant_id)}#{role}": role for role in MEMBER_ROLES}
        grants: list[Grant] = []
        for t in tuples:
            if t.relation not in GRANT_RELATIONS:
                continue
            if (subject := subject_of(t.user)) is not None:
                grants.append(Grant(subject, t.relation, resource))
            elif t.user in roles:
                grants.append(Grant(roles[t.user], t.relation, resource, subject_type=GRANT_SUBJECT_ROLE))
        return sorted(grants, key=lambda g: (g.relation, g.subject_type, g.subject))

    # --- agent-bound grants (docs/iam.md, "Agent and workflow grants") ----------------------------------------------

    @staticmethod
    def _use_key(grant: UseGrant) -> TupleKey:
        if grant.subject.kind not in USE_SUBJECT_KINDS or grant.resource.kind not in USE_RESOURCE_KINDS:
            raise GrantError("invalid_relation", "an agent or workflow may be granted an MCP server or a secret")
        if grant.subject.tenant_id != grant.resource.tenant_id:
            raise GrantError("invalid_resource", "a grant stays inside one tenant")
        return TupleKey(subject_object_ref(grant.subject), GRANTED_TO, object_ref(grant.resource))

    async def grant_use(self, grant: UseGrant) -> None:
        await self.client.write([self._use_key(grant)])

    async def revoke_use(self, grant: UseGrant) -> None:
        await self.client.write(deletes=[self._use_key(grant)])

    async def used_by(self, subject: ResourceRef, kind: str) -> list[str]:
        """The ids of `kind` the agent or workflow may use: one ListObjects (`can_use_as_agent`). An id the platform
        could not have made (another tenant) never comes back."""
        if subject.kind not in USE_SUBJECT_KINDS or kind not in USE_RESOURCE_KINDS:
            raise GrantError("invalid_relation", "an agent or workflow may use an MCP server or a secret")
        objects = await self.client.list_objects(object_ref(subject), "can_use_as_agent", kind, ())
        if len(objects) >= self.client.settings.list_limit:
            log.warning("openfga list_objects reached its limit (%s) for agent grants", self.client.settings.list_limit)
        return sorted(i for o in objects if (i := strip_object(o, kind, subject.tenant_id)) is not None)

    async def grantees(self, resource: ResourceRef) -> list[ResourceRef]:
        if resource.kind not in USE_RESOURCE_KINDS:
            raise GrantError("invalid_relation", "only an MCP server or a secret has grantees")
        found = await self.client.read({"object": object_ref(resource), "relation": GRANTED_TO})
        out: list[ResourceRef] = []
        for t in found:
            parts = split_object(t.user)
            if t.relation == GRANTED_TO and parts and parts[1] == resource.tenant_id and parts[0] in USE_SUBJECT_KINDS:
                out.append(ResourceRef(parts[0], parts[2], resource.tenant_id))
        return sorted(out, key=lambda r: (r.kind, str(r.id)))
