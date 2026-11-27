"""`python -m eap_iam_zitadel.bootstrap --platform-users` and `--instance-policy` (tenant provisioning design 4.2, 4.3).

Both need the instance owner's key (`platform-automation`), are idempotent (every step reads first and writes only a
difference) and take `--dry-run`.

**Platform users** (F3), so that nothing in the cluster has to hold the instance owner's key:

  * `platform-provisioner`: instance role `IAM_ORG_MANAGER`, for the tenant operations. It can write every user of every
    organisation, the instance owner included, so it never lives in the API pod.
  * `platform-members`: `ORG_USER_PERMISSION_EDITOR` on the operator organisation and no instance role; the API's key.
    The role writes grants and nothing else. NOT `ORG_USER_MANAGER`: the instance owner's users live in this
    organisation, and that role can mint a key for them or reset their password (proven on ZITADEL v4.20.5,
    `iam_zitadel_tests/test_contract.py`). Tenant organisations, which hold no instance owner, get `ORG_USER_MANAGER`
    when they are provisioned.

A key is printed ONCE, when it is issued, for the secret manager; it is never logged.

**Instance policy** (F5): the default login policy allows no self-registration, no external sign-up and no domain
discovery and requires a second factor; public organisation registration is off. A custom login policy of the operator
organisation does not inherit the default, so the second factor and registration-off are set there too, its other values
untouched. A second factor is always allowed before one is required, so nobody is locked out."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

from eap_iam_zitadel.management import ManagementClient, ZitadelApiError

PLACEHOLDER = "(created by the real run)"
PROJECT_NAME = "durable"
PROVISIONER = "platform-provisioner"
MEMBERS = "platform-members"
PLATFORM_USERS = {PROVISIONER: "Platform provisioner", MEMBERS: "Platform members"}
PROVISIONER_ROLES = ["IAM_ORG_MANAGER"]
MEMBERS_ROLES = ["ORG_USER_PERMISSION_EDITOR"]
KEY_SECRET_FIELD = {PROVISIONER: "EAP_ZITADEL_PROVISIONER_KEY", MEMBERS: "EAP_ZITADEL_MACHINE_KEY"}
SECOND_FACTORS = ("SECOND_FACTOR_TYPE_OTP", "SECOND_FACTOR_TYPE_U2F")
# The writable fields of a login policy (zitadel admin and management `UpdateLoginPolicy`). A GET leaves out a field
# holding its zero value, so every boolean absent from the answer is False.
POLICY_BOOLEANS = (
    "allowUsernamePassword", "allowRegister", "allowExternalIdp", "forceMfa", "hidePasswordReset",
    "ignoreUnknownUsernames", "allowDomainDiscovery", "disableLoginWithEmail", "disableLoginWithPhone", "forceMfaLocalOnly",
)
POLICY_VALUES = (
    "passwordlessType", "defaultRedirectUri", "passwordCheckLifetime", "externalLoginCheckLifetime",
    "mfaInitSkipLifetime", "secondFactorCheckLifetime", "multiFactorCheckLifetime",
)
_UNCHANGED = ("NotChanged", "has not been changed", "5M9vdd")


class SetupError(Exception):
    """A step cannot continue; the message names what to fix and never carries a credential."""


class PlatformSetup:
    def __init__(
        self,
        client: ManagementClient,
        *,
        dry_run: bool,
        emit: Callable[[str], None],
        force_mfa: bool = True,
        rotate: tuple[str, ...] = (),
        key_expires: str | None = None,
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        self.emit = emit  # where a freshly issued key goes: at once, so a later failure cannot lose it
        self.force_mfa = force_mfa
        self.rotate = rotate
        self.key_expires = key_expires
        self.changes: list[str] = []  # what was written, or (dry run) would be; each reads "<verb> ..."
        self.planned: list[str] = []  # dry run: the write calls
        self.notes: list[str] = []
        self.org_id = ""

    # --- plumbing --------------------------------------------------------------------------------------------------

    async def _read(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.client.request(method, path, org_id=self.org_id or None, json=body)

    async def _write(self, change: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.dry_run:
            self.planned.append(f"{method} {path}")
            self._changed(change)
            return {"userId": PLACEHOLDER}
        try:
            answer = await self.client.request(method, path, org_id=self.org_id or None, json=body)
        except ZitadelApiError as exc:
            if exc.status == 400 and any(marker in exc.message for marker in _UNCHANGED):
                return {}  # ZITADEL refuses a write that changes nothing
            raise
        self._changed(change)
        return answer

    def _changed(self, change: str) -> None:
        if change not in self.changes:
            self.changes.append(change)

    async def _organisation(self) -> None:
        answer = await self._read("GET", "/management/v1/orgs/me")
        org = (answer.get("org") or {}).get("id")
        if not isinstance(org, str) or not org:
            raise SetupError("ZITADEL did not name the machine user's organisation")
        self.org_id = org

    # --- platform users (F3) ---------------------------------------------------------------------------------------

    async def platform_users(self) -> None:
        await self._organisation()
        found = await self._read(
            "POST", "/management/v1/projects/_search",
            {"queries": [{"nameQuery": {"name": PROJECT_NAME, "method": "TEXT_QUERY_METHOD_EQUALS"}}]},
        )
        if not any(p.get("name") == PROJECT_NAME for p in found.get("result", [])):
            raise SetupError(
                f"the key's organisation has no project {PROJECT_NAME!r}: run the bootstrap first, with the key of a "
                "machine user of the operator organisation"
            )
        ids = {name: await self._machine_user(name) for name in PLATFORM_USERS}
        await self._provisioner_membership(ids[PROVISIONER])
        await self._members_membership(ids[MEMBERS])
        for name, user_id in ids.items():
            await self._key(name, user_id)

    async def _machine_user(self, name: str) -> str | None:
        """The user's id; None in a dry run for one that does not exist yet."""
        found = await self._read(
            "POST", "/management/v1/users/_search",
            {"queries": [{"userNameQuery": {"userName": name, "method": "TEXT_QUERY_METHOD_EQUALS"}}]},
        )
        users = [u for u in found.get("result", []) if str(u.get("userName", "")).lower() == name]
        if users:
            if "machine" not in users[0]:
                raise SetupError(f"the user {name!r} exists and is not a machine user; rename or remove it first")
            return str(users[0]["id"])
        made = await self._write(
            f"create the machine user {name}", "POST", "/management/v1/users/machine",
            {"userName": name, "name": PLATFORM_USERS[name]},
        )
        if self.dry_run:
            return None
        user_id = str(made.get("userId") or "")
        if not user_id:
            raise SetupError(f"ZITADEL did not return the id of {name!r}")
        return user_id

    async def _roles_of(self, path: str, user_id: str) -> list[str] | None:
        """The manager roles of the user's membership at `path`; None without one."""
        found = await self._read("POST", f"{path}/_search", {"queries": [{"userIdQuery": {"userId": user_id}}]})
        for member in found.get("result", []):
            if member.get("userId") == user_id:
                return [str(r) for r in member.get("roles", [])]
        return None

    async def _membership(self, path: str, user_id: str | None, roles: list[str], what: str) -> None:
        have = await self._roles_of(path, user_id) if user_id else None
        if have is None:
            await self._write(f"grant {what}", "POST", path, {"userId": user_id or PLACEHOLDER, "roles": roles})
        elif sorted(have) != sorted(roles):
            await self._write(f"replace the roles {', '.join(have)} with {what}", "PUT", f"{path}/{user_id}", {"roles": roles})

    async def _provisioner_membership(self, user_id: str | None) -> None:
        await self._membership(
            "/admin/v1/members", user_id, PROVISIONER_ROLES, f"{PROVISIONER} the instance role {PROVISIONER_ROLES[0]}"
        )

    async def _members_membership(self, user_id: str | None) -> None:
        if user_id and await self._roles_of("/admin/v1/members", user_id) is not None:
            await self._write(
                f"remove the instance membership of {MEMBERS}", "DELETE", f"/admin/v1/members/{user_id}"
            )
        await self._membership(
            "/management/v1/orgs/me/members", user_id, MEMBERS_ROLES,
            f"{MEMBERS} {MEMBERS_ROLES[0]} on the operator organisation",
        )

    async def _key(self, name: str, user_id: str | None) -> None:
        old: list[str] = []
        if user_id:
            found = await self._read("POST", f"/management/v1/users/{user_id}/keys/_search", {})
            old = [str(k.get("id")) for k in found.get("result", [])]
        if old and name not in self.rotate:
            self.notes.append(
                f"{name}: a key exists (id {', '.join(old)}), not shown; --rotate-key {name} issues a new one"
            )
            return
        body: dict[str, Any] = {"type": "KEY_TYPE_JSON"}
        if self.key_expires:
            body["expirationDate"] = self.key_expires
        made = await self._write(
            f"create a key for {name}", "POST", f"/management/v1/users/{user_id or PLACEHOLDER}/keys", body
        )
        if self.dry_run:
            return
        try:
            key = json.dumps(json.loads(base64.b64decode(made["keyDetails"])), separators=(",", ":"))
        except (KeyError, ValueError, TypeError):
            raise SetupError(f"ZITADEL did not return the new key of {name!r}") from None
        self.emit(f"{name} key (shown once; store it as {KEY_SECRET_FIELD[name]} in the secret manager):\n{key}\n")
        if old:
            self.notes.append(
                f"{name}: delete the old key (id {', '.join(old)}) in the ZITADEL console once the new one is in use"
            )

    # --- instance policy (F5, Q3) ----------------------------------------------------------------------------------

    def _wanted(self) -> dict[str, bool]:
        return {"allowRegister": False, "allowExternalIdp": False, "allowDomainDiscovery": False, "forceMfa": self.force_mfa}

    @staticmethod
    def _full(policy: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {k: bool(policy.get(k, False)) for k in POLICY_BOOLEANS}
        body.update({k: policy[k] for k in POLICY_VALUES if k in policy})
        return body

    async def _policy(self, path: str, policy: dict[str, Any], wanted: dict[str, bool], where: str) -> None:
        differs = {k: v for k, v in wanted.items() if bool(policy.get(k, False)) != v}
        if wanted.get("forceMfa") and not policy.get("secondFactors"):
            for factor in SECOND_FACTORS:
                await self._write(f"allow a second factor in {where}", "POST", f"{path}/second_factors", {"type": factor})
        if differs:
            words = {
                "allowRegister": "self-registration", "allowExternalIdp": "external identity providers",
                "allowDomainDiscovery": "domain discovery", "forceMfa": "the second factor requirement",
            }
            change = ", ".join(f"{words[k]} {'on' if v else 'off'}" for k, v in differs.items())
            await self._write(f"set in {where}: {change}", "PUT", path, {**self._full(policy), **wanted})

    async def instance_policy(self) -> None:
        answer = await self._read("GET", "/admin/v1/policies/login")
        await self._policy(
            "/admin/v1/policies/login", answer.get("policy") or {}, self._wanted(), "the instance default login policy"
        )
        restrictions = await self._read("GET", "/admin/v1/restrictions")
        if restrictions.get("disallowPublicOrgRegistration") is not True:
            await self._write(
                "turn public organisation registration off", "PUT", "/admin/v1/restrictions",
                {"disallowPublicOrgRegistration": True},
            )
        await self._organisation()
        own = await self._read("GET", "/management/v1/policies/login")
        policy = own.get("policy") or {}
        if own.get("isDefault") is True or policy.get("isDefault") is True:
            self.notes.append("the operator organisation uses the instance default login policy")
            return
        wanted = {"allowRegister": False, "forceMfa": self.force_mfa}
        await self._policy(
            "/management/v1/policies/login", policy, wanted, "the operator organisation's login policy"
        )


_FUTURE = ("create", "grant", "replace", "remove", "allow", "set", "turn")
_PAST = {"create": "created", "grant": "granted", "replace": "replaced", "remove": "removed", "allow": "allowed",
         "set": "set", "turn": "turned"}


def _done(change: str) -> str:
    verb, _, rest = change.partition(" ")
    return f"{_PAST.get(verb, verb)} {rest}"


def render(setup: PlatformSetup, *, issuer: str, calls: list[tuple[str, str, int]]) -> str:
    """The summary after the keys (which `emit` already wrote)."""
    lines: list[str] = []
    if setup.dry_run:
        lines += [
            "ZITADEL platform setup DRY RUN: only read calls were made, nothing was written.",
            f"Issuer {issuer}; organisation of the machine user: {setup.org_id}.",
            "",
            "Read calls made (method, path, HTTP status):",
            *(f"  {m:<5} {p} -> {status or 'no answer'}" for m, p, status in calls),
        ]
        if setup.changes:
            lines += ["", "A real run would:", *(f"  - {c}" for c in setup.changes)]
            lines += ["", "Write calls it would make:", *(f"  {w}" for w in setup.planned)]
        else:
            lines += ["", "A real run would change nothing (already set up)."]
    else:
        lines.append(
            "ZITADEL platform setup complete." if setup.changes else "ZITADEL platform setup: nothing to change (already set up)."
        )
        lines += [f"  - {_done(c)}" for c in setup.changes]
    if setup.notes:
        lines += ["", *setup.notes]
    return "\n".join(lines) + "\n"
