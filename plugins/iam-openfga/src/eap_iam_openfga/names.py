"""How platform ids become OpenFGA objects and users, and which relation answers which action.

Object ids carry the tenant (`agent:<tenant>/<id>`): a secret or an MCP server is named, not numbered, per tenant, so
two tenants can own the same id, and a tuple can then never be shared between tenants by accident. A list answer is
also filtered by the tenant prefix, so a stray tuple cannot make another tenant's object appear."""

from __future__ import annotations

import re

from eap_iam import GRANT_RELATIONS, GrantError, ResourceRef

from eap_iam_openfga.model import model_relations

KIND_TENANT = "tenant"
KIND_RUN = "run"
# Kinds a service registers with the engine (a run is registered with its parent; it has no owner).
REGISTERED_KINDS = ("agent", "workflow", "knowledge_base", "secret", "schedule", "mcp_server", "api_key")
ROLE_RELATIONS = ("admin", "editor", "viewer")  # tenant roles; they arrive as contextual tuples
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_id(value: object) -> bool:
    """OpenFGA ids are 1..256 characters with no whitespace, `#` or `*`; a `/` is ours (tenant separator) and a tenant id
    must not contain one."""
    return isinstance(value, str) and 1 <= len(value) <= 256 and not re.search(r"[\s#*]", value)


def valid_tenant(value: object) -> bool:
    return valid_id(value) and "/" not in str(value) and ":" not in str(value)


def user_ref(subject: str) -> str:
    if not valid_id(subject):
        raise GrantError("invalid_subject", "the subject is not a valid OpenFGA user id")
    return f"user:{subject}"


def tenant_ref(tenant_id: str) -> str:
    if not valid_tenant(tenant_id):
        raise GrantError("invalid_resource", "the tenant id is not a valid OpenFGA id")
    return f"{KIND_TENANT}:{tenant_id}"


def object_ref(resource: ResourceRef) -> str:
    """`agent:<tenant>/<id>`, or `tenant:<tenant>` for the tenant itself. Raises GrantError for an id OpenFGA refuses."""
    if resource.kind == KIND_TENANT:
        return tenant_ref(resource.tenant_id)
    if resource.kind not in model_relations():
        raise GrantError("invalid_resource", f"unknown resource kind {resource.kind!r}")
    if resource.id is None or not valid_id(resource.id):
        raise GrantError("invalid_resource", "the resource id is not a valid OpenFGA id")
    tenant_ref(resource.tenant_id)
    return f"{resource.kind}:{resource.tenant_id}/{resource.id}"


def strip_object(obj: str, kind: str, tenant_id: str) -> str | None:
    """The platform id of a listed object, or None when it is of another kind or another tenant."""
    prefix = f"{kind}:{tenant_id}/"
    return obj[len(prefix):] if obj.startswith(prefix) and len(obj) > len(prefix) else None


def split_object(obj: str) -> tuple[str, str, str] | None:
    """`kind:<tenant>/<id>` -> (kind, tenant, id); None for the tenant object or anything else."""
    kind, _, rest = obj.partition(":")
    tenant, slash, rid = rest.partition("/")
    return (kind, tenant, rid) if slash and tenant and rid else None


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


def grant_relation_ok(kind: str, relation: str) -> bool:
    return relation in GRANT_RELATIONS and relation in model_relations().get(kind, frozenset())


def is_principal_subject(creator: str | None) -> bool:
    """A creator recorded as a real principal (an e-mail, or `zitadel:<sub>`), as opposed to a legacy value such as
    `operator`, `studio:operator` or `dev:<user>`, which never becomes a principal (design 11)."""
    return bool(creator) and valid_id(creator) and (bool(_EMAIL.match(creator)) or creator.startswith("zitadel:"))
