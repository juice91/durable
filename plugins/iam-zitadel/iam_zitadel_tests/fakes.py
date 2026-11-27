"""Test doubles: a JWKS + token mint (no ZITADEL container; see README) and a stateful fake Management API.

The fake implements just the Management and v2 calls the plug-in makes, with ZITADEL's field names, behind an
`httpx2.MockTransport`, and counts every call so a test can assert a second bootstrap run wrote nothing."""

from __future__ import annotations

import base64
import itertools
import json
import time
from typing import Any

import httpx2
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://auth.example.test"
PROJECT = "proj-1"
ORG = "org-1"
TENANT = "studio"
ENV = {
    "EAP_ZITADEL_ISSUER": ISSUER,
    "EAP_ZITADEL_PROJECT_ID": PROJECT,
    "EAP_ZITADEL_ORG_TENANT_MAP": f"{ORG}={TENANT}",
}
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
MACHINE = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    return {**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "kid": kid, "alg": "RS256", "use": "sig"}


class Fetcher:
    """The JWKS fetch: `keys` is a list of (kid, key); counts its calls."""

    def __init__(self, *keys: tuple[str, rsa.RSAPrivateKey]) -> None:
        self.keys = list(keys)
        self.calls = 0

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        return {"keys": [jwk(k, kid) for kid, k in self.keys]}


def mint(
    key: Any = KEY,
    kid: str | None = "k1",
    alg: str = "RS256",
    *,
    roles: dict[str, Any] | None = None,
    org: str | None = ORG,
    **claims: Any,
) -> str:
    now = int(time.time())
    body: dict[str, Any] = {
        "iss": ISSUER, "aud": ["client-1", PROJECT], "sub": "user-1", "exp": now + 900, "iat": now, "email": "User@Example.com",
        "urn:zitadel:iam:user:resourceowner:id": org,
        "urn:zitadel:iam:org:project:roles": roles if roles is not None else {"admin": {ORG: "example.test"}},
    }
    body.update(claims)
    return jwt.encode({k: v for k, v in body.items() if v is not None}, key, algorithm=alg, headers={"kid": kid} if kid else {})


def machine_key_json() -> str:
    pem = MACHINE.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return json.dumps({"type": "serviceaccount", "userId": "machine-1", "keyId": "mk-1", "key": pem})


class FakeZitadel:
    """A stateful fake of the Management, Admin, auth and v2 APIs, enough for the membership source and the bootstrap CLI.

    Shapes follow ZITADEL v4.20.5 as observed on a real container (test_contract.py runs the same operations on both):
    enum and boolean fields holding the zero value are left out of answers, a policy write that changes nothing is HTTP
    400 `...NotChanged`, and `role_claim` builds the project-roles claim `{role: {granting org id: primary domain}}`."""

    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.requests: list[tuple[str, str]] = []
        self.projects: dict[str, dict[str, Any]] = {}
        self.roles: dict[str, list[dict[str, Any]]] = {}
        self.apps: dict[str, list[dict[str, Any]]] = {}
        self.users: dict[str, dict[str, Any]] = {
            "u-admin": {"id": "u-admin", "userName": "admin", "preferredLoginName": "admin@durable.auth.example.test",
                        "human": {"email": {"email": "admin@example.test"}, "profile": {"displayName": "Admin"}}},
        }
        self.grants: list[dict[str, Any]] = []
        self.orgs: dict[str, dict[str, str]] = {ORG: {"name": "Durable", "primaryDomain": "durable.auth.example.test"}}
        self.org_members: dict[str, dict[str, list[str]]] = {}  # org id -> user id -> manager roles
        self.iam_members: dict[str, list[str]] = {"machine-1": ["IAM_OWNER"]}
        self.keys: dict[str, list[dict[str, Any]]] = {}  # user id -> keys
        self.permissions: dict[str, list[str]] = {}  # org id -> the caller's permissions (default: may create users)
        # The instance's defaults of ZITADEL's cmd/defaults.yaml: self-registration and external sign-up are on.
        self.default_policy: dict[str, Any] = {
            "allowUsernamePassword": True, "allowRegister": True, "allowExternalIdp": True, "allowDomainDiscovery": True,
            "forceMfa": False, "passwordlessType": "PASSWORDLESS_TYPE_ALLOWED", "passwordCheckLifetime": "864000s",
            "externalLoginCheckLifetime": "864000s", "mfaInitSkipLifetime": "2592000s",
            "secondFactorCheckLifetime": "64800s", "multiFactorCheckLifetime": "43200s",
        }
        self.default_factors: list[str] = ["SECOND_FACTOR_TYPE_OTP", "SECOND_FACTOR_TYPE_U2F"]
        self.org_policies: dict[str, dict[str, Any]] = {}  # custom login policies
        self.org_factors: dict[str, list[str]] = {}
        self.restrictions: dict[str, Any] = {}
        self.invited: list[dict[str, Any]] = []
        self.down = False
        self.refuse_token = False
        self.omit_default_enums = False  # answer like real ZITADEL: enum fields holding the zero value are left out
        self.reject_no_op_put = False  # answer a PUT that changes nothing with HTTP 400 COMMAND-1m88i
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle), timeout=5)

    def mutations(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.requests if m != "GET" and not p.endswith("/_search") and p != "/oauth/v2/token"]

    def add_member(self, user_id: str, email: str, *roles: str, name: str | None = None) -> None:
        self.users[user_id] = {"id": user_id, "userName": email, "human": {"email": {"email": email}}}
        self.grants.append(self._grant(user_id, list(roles), name or email))

    def _grant(self, user_id: str, roles: list[str], name: str, org: str = ORG) -> dict[str, Any]:
        user = self.users[user_id]
        email = user["human"]["email"]["email"]
        return {"id": f"g{next(self.ids)}", "userId": user_id, "projectId": PROJECT, "roleKeys": roles,
                "state": "USER_GRANT_STATE_ACTIVE", "email": email, "displayName": name, "orgId": org}

    def add_org(self, org_id: str, name: str) -> None:
        self.orgs[org_id] = {"name": name, "primaryDomain": f"{name.lower()}.auth.example.test"}

    def grant_in(self, org_id: str, user_id: str, *roles: str) -> None:
        """A grant made in `org_id`'s context, whatever organisation the user belongs to (ZITADEL allows that)."""
        self.grants.append(self._grant(user_id, list(roles), user_id, org=org_id))

    def role_claim(self, user_id: str) -> dict[str, dict[str, str]]:
        claim: dict[str, dict[str, str]] = {}
        for grant in self.grants:
            if grant["userId"] == user_id:
                for role in grant["roleKeys"]:
                    claim.setdefault(role, {})[grant["orgId"]] = self.orgs[grant["orgId"]]["primaryDomain"]
        return claim

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if path == "/oauth/v2/token" and self.refuse_token:
            return httpx2.Response(400, json={"error": "invalid_grant", "error_description": "secret detail"})
        if path == "/oauth/v2/token":
            claims = jwt.decode(
                dict(x.split("=", 1) for x in request.content.decode().split("&"))["assertion"],
                MACHINE.public_key(), algorithms=["RS256"], audience=ISSUER,
            )
            assert claims["iss"] == "machine-1"
            return httpx2.Response(200, json={"access_token": "mgmt-token", "expires_in": 3600})
        body = json.loads(request.content) if request.content else {}
        assert request.headers["authorization"] == "Bearer mgmt-token"
        assert request.headers["host"] == "auth.example.test"
        if self.down:
            return httpx2.Response(503, json={"message": "down"})
        return self.route(method, path, body, request.headers.get("x-zitadel-orgid"))

    def route(self, method: str, path: str, body: dict[str, Any], org: str | None) -> httpx2.Response:
        ok = lambda data=None: httpx2.Response(200, json=data or {})  # noqa: E731
        here = org or ORG
        zero = lambda data: {k: v for k, v in data.items() if v not in (False, "", [], None)}  # noqa: E731
        if path == "/management/v1/orgs/me":
            return ok({"org": {"id": here, **self.orgs[here]}})
        if path == "/auth/v1/permissions/zitadel/me/_search":
            return ok({"result": self.permissions.get(here, ["org.read", "user.read", "user.write", "user.grant.write"])})
        if path == "/admin/v1/policies/login":
            current = {**self.default_policy, "isDefault": True, "secondFactors": self.default_factors}
            if method == "GET":
                return ok({"policy": zero(current)})
            wanted = {k: body.get(k, "" if isinstance(v, str) else False) for k, v in self.default_policy.items()}
            if wanted == self.default_policy:
                return httpx2.Response(400, json={"message": "Default Login Policy has not been changed (INSTANCE-5M9vdd)"})
            if wanted["forceMfa"] and not self.default_factors:
                raise AssertionError("a second factor must exist before one is forced")
            self.default_policy = wanted
            return ok()
        if path == "/admin/v1/policies/login/second_factors" and method == "POST":
            self.default_factors.append(body["type"])
            return ok()
        if path == "/admin/v1/restrictions":
            if method == "GET":
                return ok(zero({"details": {"sequence": "1"}, **self.restrictions}))
            self.restrictions.update(body)
            return ok()
        if path == "/management/v1/policies/login":
            if method == "GET":
                custom = self.org_policies.get(here)
                if custom is None:
                    return ok({"policy": zero({**self.default_policy, "isDefault": True, "secondFactors": self.default_factors}), "isDefault": True})
                return ok({"policy": zero({**custom, "secondFactors": self.org_factors.get(here, [])})})
            if method == "PUT":
                custom = self.org_policies[here]
                wanted = {k: body.get(k, "" if isinstance(v, str) else False) for k, v in custom.items()}
                if wanted == custom:
                    return httpx2.Response(400, json={"message": "Errors.Org.LoginPolicy.NotChanged (Org-5M9vdd)"})
                if wanted["forceMfa"] and not self.org_factors.get(here):
                    raise AssertionError("a second factor must exist before one is forced")
                self.org_policies[here] = wanted
                return ok()
        if path == "/management/v1/policies/login/second_factors" and method == "POST":
            self.org_factors.setdefault(here, []).append(body["type"])
            return ok()
        if path == "/admin/v1/members/_search":
            wanted_ids = [q["userIdQuery"]["userId"] for q in body.get("queries", []) if "userIdQuery" in q]
            return ok(zero({"result": [{"userId": u, "roles": r} for u, r in self.iam_members.items() if not wanted_ids or u in wanted_ids]}))
        if path == "/admin/v1/members" and method == "POST":
            if body["userId"] in self.iam_members:
                return httpx2.Response(409, json={"message": "Errors.Instance.Member.AlreadyExists"})
            self.iam_members[body["userId"]] = body["roles"]
            return ok()
        if path.startswith("/admin/v1/members/"):
            uid = path.rsplit("/", 1)[1]
            if method == "DELETE":
                self.iam_members.pop(uid, None)
            else:
                self.iam_members[uid] = body["roles"]
            return ok()
        if path.startswith("/management/v1/orgs/me/members"):
            members = self.org_members.setdefault(here, {})
            if path.endswith("/_search"):
                wanted_ids = [q["userIdQuery"]["userId"] for q in body.get("queries", []) if "userIdQuery" in q]
                return ok(zero({"result": [{"userId": u, "roles": r} for u, r in members.items() if not wanted_ids or u in wanted_ids]}))
            if method == "POST":
                if body["userId"] in members:
                    return httpx2.Response(409, json={"message": "Errors.Org.Member.AlreadyExists"})
                members[body["userId"]] = body["roles"]
                return ok()
            members[path.rsplit("/", 1)[1]] = body["roles"]
            return ok()
        if path == "/management/v1/users/machine" and method == "POST":
            uid = f"u-machine{next(self.ids)}"
            self.users[uid] = {"id": uid, "userName": body["userName"], "machine": {"name": body["name"]}, "org": here}
            return ok({"userId": uid})
        if path.startswith("/management/v1/users/") and path.endswith("/keys/_search"):
            return ok(zero({"result": self.keys.get(path.split("/")[4], [])}))
        if path.startswith("/management/v1/users/") and path.endswith("/keys") and method == "POST":
            uid = path.split("/")[4]
            kid = f"key-{next(self.ids)}"
            self.keys.setdefault(uid, []).append({"id": kid, "type": body["type"], "expirationDate": body.get("expirationDate")})
            details = json.dumps({"type": "serviceaccount", "keyId": kid, "key": f"FAKE-PRIVATE-{kid}", "userId": uid})
            return ok({"keyId": kid, "keyDetails": base64.b64encode(details.encode()).decode()})
        if path == "/management/v1/projects/_search":
            return ok({"result": list(self.projects.values()) if here == ORG else []})
        if path == "/management/v1/projects" and method == "POST":
            pid = PROJECT if not self.projects else f"proj-{next(self.ids)}"
            self.projects[pid] = {"id": pid, **body}
            return ok({"id": pid})
        if path.startswith("/management/v1/projects/"):
            parts = path.split("/")
            pid = parts[4]
            tail = "/".join(parts[5:])
            if tail == "" and method == "PUT":
                self.projects[pid].update(body)
                return ok()
            if tail == "roles/_search":
                return ok({"result": self.roles.get(pid, [])})
            if tail == "roles" and method == "POST":
                self.roles.setdefault(pid, []).append({"key": body["roleKey"]})
                return ok()
            if tail == "apps/_search":
                if self.omit_default_enums:
                    zero = {"OIDC_APP_TYPE_WEB", "OIDC_AUTH_METHOD_TYPE_BASIC", "OIDC_TOKEN_TYPE_BEARER"}
                    return ok({"result": [
                        {**a, "oidcConfig": {k: v for k, v in a["oidcConfig"].items() if not (isinstance(v, str) and v in zero)}} for a in self.apps.get(pid, [])
                    ]})
                return ok({"result": self.apps.get(pid, [])})
            if tail == "apps/oidc" and method == "POST":
                n = next(self.ids)
                app = {"id": f"app-{n}", "name": body["name"], "oidcConfig": {**{k: v for k, v in body.items() if k != "name"}, "clientId": f"client-{n}"}}
                self.apps.setdefault(pid, []).append(app)
                secret = {"clientSecret": f"secret-{n}"} if body.get("authMethodType") != "OIDC_AUTH_METHOD_TYPE_NONE" else {}
                return ok({"appId": app["id"], "clientId": f"client-{n}", **secret})
            if tail.endswith("/oidc_config") and method == "PUT":
                app = next(a for a in self.apps[pid] if a["id"] == tail.split("/")[1])
                if self.reject_no_op_put or (self.omit_default_enums and all(app["oidcConfig"].get(k) == v for k, v in body.items())):
                    return httpx2.Response(400, json={"message": "No changes (COMMAND-1m88i)"})
                app["oidcConfig"].update(body)
                return ok()
            if tail.endswith("/_generate_client_secret"):
                return ok({"clientSecret": f"rotated-{next(self.ids)}"})
        if path == "/management/v1/users/_search":
            queries = body.get("queries", [])
            users = list(self.users.values())
            for q in queries:
                if "emailQuery" in q:
                    want = q["emailQuery"]["emailAddress"].lower()
                    users = [u for u in users if "human" in u and u["human"]["email"]["email"].lower() == want]
                if "userNameQuery" in q:
                    want = q["userNameQuery"]["userName"].lower()
                    exact = q["userNameQuery"].get("method") == "TEXT_QUERY_METHOD_EQUALS"
                    users = [u for u in users if (u["userName"].lower() == want if exact else u["userName"].lower().startswith(want))]
            return ok({"result": users})
        if path == "/management/v1/users/grants/_search":
            grants = self.grants
            for q in body.get("queries", []):
                if "userIdQuery" in q:
                    grants = [g for g in grants if g["userId"] == q["userIdQuery"]["userId"]]
                if "projectIdQuery" in q:
                    grants = [g for g in grants if g["projectId"] == q["projectIdQuery"]["projectId"]]
            return ok({"result": grants})
        if path == "/v2/users/human" and method == "POST":
            if "user.write" not in self.permissions.get(body["organization"]["orgId"], ["user.write"]):
                return httpx2.Response(403, json={"message": "No matching permissions found (AUTH-5mWD2)"})
            uid = f"u-new{next(self.ids)}"
            self.users[uid] = {"id": uid, "userName": body["username"], "human": {"email": {"email": body["email"]["email"]}}}
            self.invited.append(body)
            return ok({"userId": uid})
        if path.startswith("/management/v1/users/"):
            parts = path.split("/")
            uid, tail = parts[4], parts[5:]
            if tail == ["grants"] and method == "POST":
                self.grants.append(self._grant(uid, body["roleKeys"], self.users[uid]["human"]["email"]["email"], org=here))
                return ok()
            if len(tail) == 2 and tail[0] == "grants":
                grant = next((g for g in self.grants if g["id"] == tail[1] and g["userId"] == uid), None)
                if grant is None:
                    return httpx2.Response(404, json={"message": "grant not found"})
                if method == "PUT":
                    grant["roleKeys"] = body["roleKeys"]
                else:
                    self.grants.remove(grant)
                return ok()
        return httpx2.Response(404, json={"message": f"unhandled {method} {path}"})
