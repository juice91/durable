"""How platform ids become OpenFGA objects and users, and which relation answers which action.

Object ids carry the tenant (`agent:<tenant>/<id>`): a secret or an MCP server is named, not numbered, per tenant, so
two tenants can own the same id, and a tuple can then never be shared between tenants by accident. A list answer is
also filtered by the tenant prefix, so a stray tuple cannot make another tenant's object appear.

OpenFGA allows one `:` in `type:id`, and platform ids have their own (`zitadel:<sub>`). `encode_id` / `decode_id` are
the one translation, and this module is the only place that builds an OpenFGA ref from a platform id or reads one back
(docs/iam.md, "Ids in OpenFGA")."""

from __future__ import annotations

import re
from typing import Any

from eap_iam import GRANT_RELATIONS, MEMBER_ROLES, AccessRequirement, GrantableRelation, GrantError, ResourceRef

from eap_iam_openfga.model import model_json, model_relations

KIND_TENANT = "tenant"
KIND_RUN = "run"
# Kinds a service registers with the engine (a run is registered with its parent; it has no owner).
REGISTERED_KINDS = ("agent", "workflow", "knowledge_base", "secret", "schedule", "mcp_server", "api_key")
ROLE_RELATIONS = ("admin", "editor", "viewer")  # tenant roles; they arrive as contextual tuples
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# The server's limits (v1.23.0): `type:id` of an object in characters, of a user in bytes.
MAX_OBJECT_CHARS = 256
MAX_USER_BYTES = 512
_REFUSED = re.compile(r"[\s#*\x00-\x1f\x7f-\x9f\ud800-\udfff]")
_ENCODED = re.compile(r"(?:[^%:]|%25|%3A)+")
_ESCAPE = re.compile(r"%25|%3A")


def valid_id(value: object) -> bool:
    """A platform id the encoding can carry: a non-empty string with no whitespace, control character, lone surrogate,
    `#` (a userset) or `*` (a wildcard). Length is checked on the encoded ref."""
    return isinstance(value, str) and bool(value) and not _REFUSED.search(value)


def encode_id(raw: str) -> str:
    """`%` -> `%25`, then `:` -> `%3A`; every other character stays as it is. The caller checks `valid_id` first."""
    return raw.replace("%", "%25").replace(":", "%3A")


def decode_id(encoded: str) -> str | None:
    """The platform id, or None for a string `encode_id` cannot have produced (a raw `:`, another `%` sequence): such a
    stored id matches nothing the platform asks about, so it is never reported as a platform id."""
    if not _ENCODED.fullmatch(encoded) or not valid_id(encoded):
        return None
    return _ESCAPE.sub(lambda m: "%" if m.group() == "%25" else ":", encoded)


def _fits(ref: str, code: str, *, user: bool) -> str:
    if (len(ref.encode()) > MAX_USER_BYTES) if user else (len(ref) > MAX_OBJECT_CHARS):
        limit = f"{MAX_USER_BYTES} bytes" if user else f"{MAX_OBJECT_CHARS} characters"
        raise GrantError(code, f"the id is too long for OpenFGA (more than {limit} once encoded)")
    return ref


def valid_tenant(value: object) -> bool:
    """A tenant id is used as it is: it may hold neither `:` nor `/` (the separator of an object id)."""
    return valid_id(value) and "/" not in str(value) and ":" not in str(value) and len(f"{KIND_TENANT}:{value}") <= MAX_OBJECT_CHARS


def user_ref(subject: str) -> str:
    """`user:<encoded subject>`. Raises GrantError for a subject OpenFGA cannot hold."""
    if not valid_id(subject):
        raise GrantError("invalid_subject", "the subject is not a valid OpenFGA user id")
    return _fits(f"user:{encode_id(subject)}", "invalid_subject", user=True)


def subject_of(ref: str) -> str | None:
    """The platform subject of a `user:<id>` ref, or None for a userset, another type or a foreign encoding."""
    kind, _, encoded = ref.partition(":")
    return decode_id(encoded) if kind == "user" else None


def tenant_ref(tenant_id: str) -> str:
    if not valid_tenant(tenant_id):
        raise GrantError("invalid_resource", "the tenant id is not a valid OpenFGA id")
    return f"{KIND_TENANT}:{tenant_id}"


def object_ref(resource: ResourceRef) -> str:
    """`agent:<tenant>/<encoded id>`, or `tenant:<tenant>` for the tenant itself. Raises GrantError for an id OpenFGA
    cannot hold."""
    if resource.kind == KIND_TENANT:
        return tenant_ref(resource.tenant_id)
    if resource.kind not in model_relations():
        raise GrantError("invalid_resource", f"unknown resource kind {resource.kind!r}")
    if resource.id is None or not valid_id(resource.id):
        raise GrantError("invalid_resource", "the resource id is not a valid OpenFGA id")
    tenant_ref(resource.tenant_id)
    return _fits(f"{resource.kind}:{resource.tenant_id}/{encode_id(resource.id)}", "invalid_resource", user=False)


def subject_object_ref(resource: ResourceRef) -> str:
    """An object that also stands as the user of a tuple (an agent or workflow: a run's parent, an agent-bound grant):
    it must fit both limits."""
    return _fits(object_ref(resource), "invalid_resource", user=True)


def strip_object(obj: str, kind: str, tenant_id: str) -> str | None:
    """The platform id of a listed object, or None when it is of another kind or another tenant."""
    prefix = f"{kind}:{tenant_id}/"
    return decode_id(obj[len(prefix):]) if obj.startswith(prefix) else None


def split_object(obj: str) -> tuple[str, str, str] | None:
    """`kind:<tenant>/<encoded id>` -> (kind, tenant, platform id); None for the tenant object or anything else."""
    kind, _, rest = obj.partition(":")
    tenant, slash, encoded = rest.partition("/")
    rid = decode_id(encoded) if slash and tenant else None
    return None if rid is None else (kind, tenant, rid)


def relation_for(action: str, kind: str, has_id: bool) -> str | None:
    """The model relation that answers `action` on `kind`, or None when the model has none (the engine then denies).

    `agent.publish` and `publish` are the same action. A collection (`id` None) answers only `create`, against the
    tenant: `can_create_<kind>`."""
    verb = action.rsplit(".", 1)[-1]
    relations = model_relations()
    if kind == KIND_TENANT:
        name = f"can_{verb}"
        return name if name in relations[KIND_TENANT] else None
    if not has_id:
        name = f"can_create_{kind}"
        return name if verb == "create" and name in relations[KIND_TENANT] else None
    name = f"can_{verb}"
    return name if name in relations.get(kind, frozenset()) else None


def _type(kind: str) -> dict[str, Any]:
    return next((t for t in model_json()["type_definitions"] if t["type"] == kind), {})


def grantable_relations(kind: str) -> list[GrantableRelation]:
    """The grants a `kind` can hold, in `GRANT_RELATIONS` order: the relations the model lets a user be written to
    directly, and for each the tenant roles it lets be written (`tenant#<role>`)."""
    meta = (_type(kind).get("metadata") or {}).get("relations") or {}
    found: list[GrantableRelation] = []
    for relation in GRANT_RELATIONS:
        direct = (meta.get(relation) or {}).get("directly_related_user_types") or []
        if kind == KIND_TENANT or not any(d == {"type": "user"} for d in direct):
            continue
        roles = {d.get("relation") for d in direct if d.get("type") == KIND_TENANT}
        found.append(GrantableRelation(relation, tuple(r for r in MEMBER_ROLES if r in roles)))
    return found


def grant_relation_ok(kind: str, relation: str, role: bool = False) -> bool:
    return any(g.relation == relation and (g.roles or not role) for g in grantable_relations(kind))


def requirements(action: str, kind: str, has_id: bool) -> AccessRequirement | None:
    """What the model lets `action` through: walk `can_<verb>` down to the tenant roles and the grantable relations."""
    relation = relation_for(action, kind, has_id)
    if relation is None:
        return None
    start = KIND_TENANT if kind == KIND_TENANT or not has_id else kind
    grantable = {g.relation for g in grantable_relations(start)}
    roles: set[str] = set()
    relations: set[str] = set()
    seen: set[tuple[str, str]] = set()

    def rule_of(type_: str, node: dict[str, Any], name: str) -> None:
        if "this" in node:
            if type_ != KIND_TENANT and name in grantable:
                relations.add(name)
        elif "computedUserset" in node:
            walk(type_, node["computedUserset"]["relation"])
        elif "tupleToUserset" in node:
            via = node["tupleToUserset"]["tupleset"]["relation"]
            if via == KIND_TENANT:
                walk(KIND_TENANT, node["tupleToUserset"]["computedUserset"]["relation"])
        elif "union" in node:
            for child in node["union"]["child"]:
                rule_of(type_, child, name)

    def walk(type_: str, name: str) -> None:
        if (type_, name) in seen:
            return
        seen.add((type_, name))
        if type_ == KIND_TENANT and name in MEMBER_ROLES:
            roles.add(name)
        node = _type(type_).get("relations", {}).get(name)
        if node is not None:
            rule_of(type_, node, name)

    walk(start, relation)
    return AccessRequirement(
        roles=tuple(r for r in MEMBER_ROLES if r in roles),
        relations=tuple(r for r in GRANT_RELATIONS if r in relations),
    )


# Providers whose subject is `<provider>:<stable id>`: a principal, and never an e-mail whatever follows the prefix.
SUBJECT_PREFIXES = ("zitadel:", "localadmin:")


def is_email_subject(subject: str | None) -> bool:
    """A subject that is an e-mail address: how the legacy `cfaccess` provider names a person. Every other provider uses
    an id (`zitadel:<sub>`, a key id, the dev bearer's user)."""
    return bool(subject) and not str(subject).startswith(SUBJECT_PREFIXES) and bool(_EMAIL.match(str(subject)))


def is_principal_subject(creator: str | None) -> bool:
    """A creator recorded as a real principal (an e-mail, `zitadel:<sub>` or `localadmin:<username>`), as opposed to a legacy value such as
    `operator`, `studio:operator` or `dev:<user>`, which never becomes a principal (design 11)."""
    if not creator or not (_EMAIL.match(creator) or creator.startswith(SUBJECT_PREFIXES)):
        return False
    try:
        user_ref(creator)
    except GrantError:
        return False
    return True
