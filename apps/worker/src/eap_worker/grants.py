"""Agent-bound grants at run time (docs/iam.md, "Agent and workflow grants").

An admin authorises an agent or a workflow to use MCP servers and named secrets. When a run STARTS the worker asks the policy
engine once what its subject (the agent, or for a workflow run the workflow) may use, stores the answer on the run
(`runs.grant_set`) and every gate reads that stored set:

- an MCP tool call (and the listing of a server's tools): the server must be in the set; the server's own bound credentials
  (sealed headers, env values, `secret:<name>` in a managed server's env) come with the server grant;
- a `secret:<name>` in a built-in or MCP tool argument, and in `http.request`: the name must be in the set, checked
  BEFORE any value is read (a refusal is no oracle for whether a secret exists).

A refusal is a tool error to the model with the stable code `permission_denied` and the readable message "agent X is not
authorized to use MCP server Y", plus a `permission_denied` run event; it never fails the run by itself.

A child run (an agent used as a tool) takes the intersection with its parent agent run's set. A workflow's agent step is its
own agent run: it uses the agent's grants, not the workflow's. The engine is not on the tool-call path: a run in flight keeps
the set it started with, and when the engine cannot answer at start the run gets an empty set marked `unavailable` (fail
closed: nothing gated is allowed). Nothing is stored, and nothing is gated, while enforcement is off (`EAP_AGENT_GRANTS_ENFORCE`
unset, or the selected engine keeps no grants): every run behaves as it did before."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from eap_iam import ResourceRef, enforcement_requested, load_policy, supports_grants
from eap_schemas.secrets import is_secret_ref

log = logging.getLogger("eap_worker.grants")

PERMISSION_DENIED = "permission_denied"
EVENT_TYPE = "permission_denied"
NOT_GRANTED = "not_granted"
AUTHZ_UNAVAILABLE = "authz_unavailable"
_CACHE_SIZE = 2048

_engine: Any = None


def set_policy_engine(engine: Any) -> None:
    """The engine the start step asks (a seam for tests; production builds it from the environment on first use)."""
    global _engine
    _engine = engine
    _cache.clear()


def policy_engine() -> Any:
    global _engine
    if _engine is None:
        _engine = load_policy()
    return _engine


@dataclass(frozen=True)
class Denial:
    resource_kind: str  # mcp_server | secret
    name: str
    reason: str  # not_granted | authz_unavailable
    message: str


@dataclass(frozen=True)
class RunGrants:
    subject_kind: str
    subject_id: str
    subject_name: str
    mcp_servers: frozenset[str]  # server ids
    secrets: frozenset[str]  # secret names
    unavailable: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RunGrants:
        subject = data.get("subject") if isinstance(data.get("subject"), dict) else {}
        return cls(
            str(subject.get("kind") or "agent"), str(subject.get("id") or ""), str(subject.get("name") or subject.get("id") or ""),
            frozenset(str(i) for i in data.get("mcp_servers") or ()), frozenset(str(i) for i in data.get("secrets") or ()),
            bool(data.get("unavailable")),
        )

    def _denial(self, resource_kind: str, name: str) -> Denial:
        what = f"MCP server {name}" if resource_kind == "mcp_server" else f"secret {name}"
        if self.unavailable:
            return Denial(resource_kind, name, AUTHZ_UNAVAILABLE, f"authorization is unavailable, so {self.subject_kind} {self.subject_name} cannot use {what} now")
        return Denial(resource_kind, name, NOT_GRANTED, f"{self.subject_kind} {self.subject_name} is not authorized to use {what}")

    def deny_server(self, server_id: str, server_name: str) -> Denial | None:
        return None if server_id in self.mcp_servers else self._denial("mcp_server", server_name)

    def deny_secrets(self, names: Iterable[str]) -> Denial | None:
        for name in sorted(set(names)):
            if name not in self.secrets:
                return self._denial("secret", name)
        return None


def secret_names_in(value: Any) -> list[str]:
    """Every `secret:<name>` string in a tool argument (nested dicts and lists), sorted, each once."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            name = is_secret_ref(node)
            if name is not None:
                found.add(name)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(value)
    return sorted(found)


# -- reading the stored set ---------------------------------------------------------------------------------------------

_cache: OrderedDict[tuple[str, str], RunGrants | None] = OrderedDict()


def run_grants(tenant_id: str, run_id: str | UUID | None) -> RunGrants | None:
    """The run's start-time set, or None when nothing is enforced for it (blocking: one read per run per process)."""
    if run_id is None or not enforcement_requested():
        return None
    key = (tenant_id, str(run_id))
    if key in _cache:
        return _cache[key]
    from eap_worker.runtime import get_runtime

    data = get_runtime().run_store.get_grant_set(tenant_id, UUID(str(run_id)))
    found = RunGrants.from_json(data) if isinstance(data, dict) and data.get("enforced") else None
    _cache[key] = found
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return found


def denial_for_server(tenant_id: str, run_id: str | UUID | None, server_id: str, server_name: str) -> Denial | None:
    grants = run_grants(tenant_id, run_id)
    return None if grants is None else grants.deny_server(server_id, server_name)


def denial_for_secrets(tenant_id: str, run_id: str | UUID | None, names: Iterable[str]) -> Denial | None:
    names = list(names)
    if not names:
        return None
    grants = run_grants(tenant_id, run_id)
    return None if grants is None else grants.deny_secrets(names)


def record_denial(tenant_id: str, run_id: str | UUID | None, denial: Denial) -> None:
    """The `permission_denied` run event (one per resource per run; names only, never a value). Best effort."""
    from eap_worker.runtime import get_runtime

    if run_id is None:
        return
    grants = run_grants(tenant_id, run_id)
    data = {
        "code": PERMISSION_DENIED, "reason": denial.reason, "resource_kind": denial.resource_kind, "name": denial.name,
        "message": denial.message,
        **({"subject_kind": grants.subject_kind, "subject_id": grants.subject_id} if grants is not None else {}),
    }
    try:
        get_runtime().event_log.append(
            tenant_id, UUID(str(run_id)), f"permission_denied:{denial.resource_kind}:{denial.name}", EVENT_TYPE, data
        )
    except Exception:  # noqa: BLE001 - the refusal itself stands; the event is advice
        log.warning("permission_denied event not recorded run=%s", run_id, exc_info=True)
    log.warning("permission_denied run=%s kind=%s name=%s reason=%s", run_id, denial.resource_kind, denial.name, denial.reason)


def refuse_message(denial: Denial) -> str:
    return f"{PERMISSION_DENIED}: {denial.message}"


# -- computing the set at run start -------------------------------------------------------------------------------------


async def ensure_grant_set(
    rt: Any, tenant_id: str, run_id: str, subject_kind: str, subject_id: str, subject_name: str | None
) -> dict[str, Any] | None:
    """Compute and store the run's start-time set once (a replay or a second start returns the stored one). None when
    enforcement is off. Never raises: an engine that cannot answer gives an empty set marked `unavailable`."""
    if not enforcement_requested():
        return None
    run_uuid = UUID(str(run_id))
    existing = await asyncio.to_thread(rt.run_store.get_grant_set, tenant_id, run_uuid)
    if existing is not None:
        return existing
    unavailable = False
    servers: set[str] = set()
    secrets: set[str] = set()
    try:
        engine = policy_engine()
        if not supports_grants(engine):
            return None  # an engine with no grants (`openaccess`): nothing to enforce, as before
        subject = ResourceRef(subject_kind, subject_id, tenant_id)
        servers = set(await engine.used_by(subject, "mcp_server"))
        secrets = set(await engine.used_by(subject, "secret"))
    except Exception as exc:  # noqa: BLE001 - fail closed for this run; the type is logged, never the detail
        unavailable = True
        servers, secrets = set(), set()
        log.error("grant set unavailable run=%s error_type=%s", run_id, type(exc).__name__)
    parent = await asyncio.to_thread(_parent_set, rt, tenant_id, run_uuid) if subject_kind == "agent" else None
    if parent is not None:  # a child of an agent run: what both may use
        servers &= set(parent.get("mcp_servers") or ())
        secrets &= set(parent.get("secrets") or ())
        unavailable = unavailable or bool(parent.get("unavailable"))
    document = {
        "enforced": True, "subject": {"kind": subject_kind, "id": subject_id, "name": subject_name or subject_id},
        "mcp_servers": sorted(servers), "secrets": sorted(secrets), "unavailable": unavailable,
    }
    stored = await asyncio.to_thread(rt.run_store.claim_grant_set, tenant_id, run_uuid, document)
    _cache.pop((tenant_id, str(run_id)), None)
    return stored


def _parent_set(rt: Any, tenant_id: str, run_id: UUID) -> dict[str, Any] | None:
    run = rt.run_store.get_run(tenant_id, run_id) or {}
    parent_id = run.get("parent_run_id")
    if parent_id is None:
        return None
    parent = rt.run_store.get_run(tenant_id, UUID(str(parent_id))) or {}
    if parent.get("kind") != "agent":
        return None  # a workflow's agent step uses the agent's own grants; evals and tools carry none
    data = rt.run_store.get_grant_set(tenant_id, UUID(str(parent_id)))
    return data if isinstance(data, dict) and data.get("enforced") else None
