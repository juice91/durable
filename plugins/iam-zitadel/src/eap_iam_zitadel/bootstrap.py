"""`python -m eap_iam_zitadel.bootstrap`: set ZITADEL up for the platform, idempotently (design 11, bootstrap step 2).

Creates (or finds) in the machine user's organisation:
  - the project `durable` with the roles admin, editor and viewer (role assertion on, role check on: a user who holds no
    grant cannot sign in to the project's applications, so removing a member really removes the access);
  - the Studio application: public OIDC client, authorization code + PKCE, with the redirect and post-logout URIs;
  - the admin role for the bootstrap admin user.
Then prints the org id -> tenant mapping and the values to add to the deploy secrets.

Only with `--cloudflare-access TEAM` it also creates (or repairs) a confidential OIDC application for Cloudflare Access
to use as its identity provider, and prints the Cloudflare setup. Without the option it neither reads nor writes such an
application: one that exists is kept (it is never deleted here).

A second run changes nothing: every step first reads what exists and writes only a difference. The client ids and the
Access client secret go to STDOUT only, as instructions for the operator; nothing is logged, and the secret is shown
once, when ZITADEL issues it (`--rotate-access-secret` issues a new one).

`--platform-users` and `--instance-policy` do something else (and only that): the split machine users and the instance
login policy of the tenant provisioning design; see `platform_setup`."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TextIO

import httpx2

from eap_iam_zitadel import platform_setup
from eap_iam_zitadel.management import ManagementClient, ZitadelApiError
from eap_iam_zitadel.settings import parse_machine_key

PLACEHOLDER = "(created by the real run)"
API_VERSION = "ZITADEL v4.20.5: Management API v1 (/management/v1/...) and the v2 user service (/v2/users/human)"
PROJECT_NAME = "durable"
ROLES = (("admin", "Admin"), ("editor", "Editor"), ("viewer", "Viewer"))
STUDIO_APP = "Durable Studio"
ACCESS_APP = "Cloudflare Access"
DEFAULT_ISSUER = "https://auth.example-org.ai"
DEFAULT_PUBLIC_URL = "https://durable.example-org.ai"
DEFAULT_LOOPBACK_URL = "http://127.0.0.1:13000"
EQUALS = "TEXT_QUERY_METHOD_EQUALS"
EQUALS_IC = "TEXT_QUERY_METHOD_EQUALS_IGNORE_CASE"

STUDIO_CONFIG_KEYS = (
    "redirectUris", "postLogoutRedirectUris", "responseTypes", "grantTypes", "appType", "authMethodType", "devMode",
    "accessTokenType", "accessTokenRoleAssertion", "idTokenRoleAssertion", "idTokenUserinfoAssertion",
)
ACCESS_CONFIG_KEYS = ("redirectUris", "responseTypes", "grantTypes", "appType", "authMethodType", "accessTokenType")


class BootstrapError(Exception):
    """A step cannot continue; the message names what to fix and never carries a credential."""


@dataclass
class Result:
    org_id: str = ""
    project_id: str = ""
    studio_client_id: str = ""
    access_client_id: str = ""
    access_client_secret: str | None = None  # only on the run that created (or rotated) it
    admin_user_id: str = ""
    changes: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)  # dry run: what WOULD be written


def studio_config(public_url: str, loopback_url: str) -> dict[str, Any]:
    public, loopback = public_url.rstrip("/"), loopback_url.rstrip("/")
    return {
        "redirectUris": [f"{public}/auth/callback", f"{loopback}/auth/callback"],
        "postLogoutRedirectUris": [f"{public}/", f"{loopback}/"],
        "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
        "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE", "OIDC_GRANT_TYPE_REFRESH_TOKEN"],
        "appType": "OIDC_APP_TYPE_USER_AGENT",
        "authMethodType": "OIDC_AUTH_METHOD_TYPE_NONE",  # a public client: PKCE instead of a secret
        "devMode": True,  # ZITADEL accepts an http:// redirect (the SSH-tunnel loopback page) only in dev mode
        "accessTokenType": "OIDC_TOKEN_TYPE_JWT",
        "accessTokenRoleAssertion": True,
        "idTokenRoleAssertion": True,
        "idTokenUserinfoAssertion": True,
    }


def access_config(team: str) -> dict[str, Any]:
    return {
        "redirectUris": [f"https://{team}.cloudflareaccess.com/cdn-cgi/access/callback"],
        "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
        "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"],
        "appType": "OIDC_APP_TYPE_WEB",
        "authMethodType": "OIDC_AUTH_METHOD_TYPE_BASIC",  # a confidential client; Cloudflare sends the secret
        "accessTokenType": "OIDC_TOKEN_TYPE_BEARER",
    }


# ZITADEL's JSON responses OMIT enum fields holding the proto zero value. These are the zero values (enum value 0) of
# zitadel/app.proto: OIDCAppType.OIDC_APP_TYPE_WEB = 0, OIDCAuthMethodType.OIDC_AUTH_METHOD_TYPE_BASIC = 0 and
# OIDCTokenType.OIDC_TOKEN_TYPE_BEARER = 0. A missing key therefore means that value, not "different".
_ENUM_ZERO = {
    "appType": "OIDC_APP_TYPE_WEB",
    "authMethodType": "OIDC_AUTH_METHOD_TYPE_BASIC",
    "accessTokenType": "OIDC_TOKEN_TYPE_BEARER",
}


def _same(existing: Mapping[str, Any], desired: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    for key in keys:
        if key not in desired:
            continue
        have, want = existing.get(key), desired[key]
        if have is None and key in _ENUM_ZERO:
            have = _ENUM_ZERO[key]
        if isinstance(want, list):
            if sorted(have or []) != sorted(want):
                return False
        elif have != want and not (have is None and want is False):
            return False
    return True


class Bootstrap:
    def __init__(self, client: ManagementClient, *, tenant: str, public_url: str, loopback_url: str, access_team: str | None,
                 admin_user: str, rotate_access_secret: bool = False, dry_run: bool = False) -> None:
        self.client = client
        self.tenant = tenant
        self.studio = studio_config(public_url, loopback_url)
        self.access = access_config(access_team) if access_team else None  # None: not asked for
        self.admin_user = admin_user
        self.rotate = rotate_access_secret
        self.dry_run = dry_run
        self._project_exists = True
        self.result = Result()

    async def run(self) -> Result:
        res = self.result
        res.org_id = await self._org()
        res.project_id = await self._project()
        await self._roles()
        res.studio_client_id = await self._app(STUDIO_APP, self.studio, STUDIO_CONFIG_KEYS, public=True)
        if self.access:
            res.access_client_id = await self._app(ACCESS_APP, self.access, ACCESS_CONFIG_KEYS, public=False)
        res.admin_user_id = await self._admin_grant()
        return res

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        is_read = method == "GET" or path.endswith("/_search")
        if self.dry_run and not is_read:
            # A dry run performs READ calls only; a write is recorded as planned and answered with a placeholder.
            self.result.planned.append(f"{method} {path}")
            return {"id": PLACEHOLDER, "appId": PLACEHOLDER, "clientId": PLACEHOLDER, "clientSecret": None}
        try:
            return await self.client.request(method, path, org_id=self.result.org_id or None, json=body)
        except ZitadelApiError as exc:
            raise BootstrapError(str(exc)) from None  # names the call and the HTTP status, never a credential

    async def _org(self) -> str:
        answer = await self._call("GET", "/management/v1/orgs/me")
        org = (answer.get("org") or {}).get("id")
        if not isinstance(org, str) or not org:
            raise BootstrapError("ZITADEL did not name the machine user's organisation")
        return org

    async def _project(self) -> str:
        found = await self._call(
            "POST", "/management/v1/projects/_search",
            {"queries": [{"nameQuery": {"name": PROJECT_NAME, "method": EQUALS}}]},
        )
        wanted = {"projectRoleAssertion": True, "projectRoleCheck": True}
        for item in found.get("result", []):
            if item.get("name") == PROJECT_NAME:
                if not _same(item, wanted, tuple(wanted)):
                    await self._call("PUT", f"/management/v1/projects/{item['id']}", {"name": PROJECT_NAME, **wanted})
                    self.result.changes.append("updated project settings")
                return str(item["id"])
        made = await self._call("POST", "/management/v1/projects", {"name": PROJECT_NAME, **wanted})
        self.result.changes.append("created project")
        self._project_exists = False if self.dry_run else True
        return str(made["id"])

    async def _roles(self) -> None:
        pid = self.result.project_id
        have = await self._call("POST", f"/management/v1/projects/{pid}/roles/_search", {}) if self._project_exists else {}
        keys = {r.get("key") for r in have.get("result", [])}
        for key, label in ROLES:
            if key not in keys:
                await self._call(
                    "POST", f"/management/v1/projects/{pid}/roles", {"roleKey": key, "displayName": label, "group": PROJECT_NAME}
                )
                self.result.changes.append(f"created role {key}")

    async def _app(self, name: str, desired: dict[str, Any], keys: tuple[str, ...], *, public: bool) -> str:
        pid = self.result.project_id
        found = (
            await self._call(
                "POST", f"/management/v1/projects/{pid}/apps/_search",
                {"queries": [{"nameQuery": {"name": name, "method": EQUALS}}]},
            )
            if self._project_exists
            else {}
        )
        for app in found.get("result", []):
            if app.get("name") != name:
                continue
            config = app.get("oidcConfig") or {}
            client_id = str(config.get("clientId") or "")
            if not client_id:
                raise BootstrapError(f"the application {name!r} exists but is not an OIDC application")
            if not _same(config, desired, keys):
                try:
                    await self._call("PUT", f"/management/v1/projects/{pid}/apps/{app['id']}/oidc_config", desired)
                    self.result.changes.append(f"updated application {name}")
                except BootstrapError as exc:
                    # Defence in depth: ZITADEL rejects a PUT that changes nothing (HTTP 400, COMMAND-1m88i).
                    if "COMMAND-1m88i" not in str(exc) and "No changes" not in str(exc):
                        raise
            if not public and self.rotate:
                made = await self._call(
                    "POST", f"/management/v1/projects/{pid}/apps/{app['id']}/oidc_config/_generate_client_secret", {}
                )
                self.result.access_client_secret = made.get("clientSecret") or None
                self.result.changes.append(f"rotated the client secret of {name}")
            return client_id
        made = await self._call("POST", f"/management/v1/projects/{pid}/apps/oidc", {"name": name, **desired})
        self.result.changes.append(f"created application {name}")
        if not public:
            self.result.access_client_secret = made.get("clientSecret") or None
        client_id = str(made.get("clientId") or "")
        if not client_id:
            raise BootstrapError(f"ZITADEL did not return a client id for {name!r}")
        return client_id

    async def _admin_grant(self) -> str:
        want = self.admin_user.strip().lower()
        found = await self._call(
            "POST", "/management/v1/users/_search",
            {"queries": [{"userNameQuery": {"userName": want.split("@")[0], "method": "TEXT_QUERY_METHOD_STARTS_WITH_IGNORE_CASE"}}]},
        )
        def names(u: Mapping[str, Any]) -> set[str]:
            logins = [str(u.get("preferredLoginName", "")), *map(str, u.get("loginNames") or [])]
            local = {x.split("@")[0] for x in logins if x} if "@" not in want else set()  # exact local part of a login
            return {str(u.get("userName", "")).lower(), *(x.lower() for x in logins),
                    str((u["human"].get("email") or {}).get("email", "")).lower(), *(x.lower() for x in local)}

        users = [u for u in found.get("result", []) if isinstance(u.get("human"), dict) and want in names(u)]
        if len(users) != 1:
            raise BootstrapError(
                f"found {len(users)} human users matching {self.admin_user!r} in the organisation; "
                "pass the bootstrap admin's login name or e-mail with --admin-user"
            )
        user_id = str(users[0]["id"])
        pid = self.result.project_id
        grants = (
            await self._call(
                "POST", "/management/v1/users/grants/_search",
                {"queries": [{"projectIdQuery": {"projectId": pid}}, {"userIdQuery": {"userId": user_id}}]},
            )
            if self._project_exists
            else {}
        )
        mine = [g for g in grants.get("result", []) if g.get("userId") == user_id]
        if not mine:
            await self._call("POST", f"/management/v1/users/{user_id}/grants", {"projectId": pid, "roleKeys": ["admin"]})
            self.result.changes.append("granted admin to the bootstrap admin")
        elif "admin" not in mine[0].get("roleKeys", []):
            roles = sorted({*mine[0].get("roleKeys", []), "admin"})
            await self._call("PUT", f"/management/v1/users/{user_id}/grants/{mine[0]['id']}", {"roleKeys": roles})
            self.result.changes.append("added the admin role to the bootstrap admin")
        return user_id


_FUTURE = (("created", "create"), ("updated", "update"), ("granted", "grant"), ("added", "add"), ("rotated", "rotate"))


def _would(change: str) -> str:
    for past, present in _FUTURE:
        if change.startswith(past):
            return present + change[len(past):]
    return change


def render_dry_run(res: Result, *, issuer: str, calls: list[tuple[str, str, int]]) -> str:
    """What a dry run printed: the API it talked to, every READ call with its HTTP status, and what a real run would do."""
    lines = [
        "ZITADEL bootstrap DRY RUN: only read calls were made, nothing was written.",
        f"API: {API_VERSION}; issuer {issuer}.",
        "",
        "Read calls made (method, path, HTTP status):",
        *(f"  {m:<5} {p} -> {status or 'no answer'}" for m, p, status in calls),
        "",
        f"Organisation of the machine user: {res.org_id}",
    ]
    if res.changes:
        lines += ["", "A real run would:", *(f"  - {_would(c)}" for c in res.changes)]
        if res.planned:
            lines += ["", "Write calls it would make:", *(f"  {w}" for w in res.planned)]
    else:
        lines += ["", "A real run would change nothing (already set up)."]
    return "\n".join(lines) + "\n"


def render(res: Result, *, issuer: str, tenant: str, access_team: str | None = None) -> str:
    """The operator's instructions. With `_access_setup`, the ONLY place the client ids and the Access secret are written."""
    lines = [
        "ZITADEL bootstrap complete." if res.changes else "ZITADEL bootstrap: nothing to change (already set up).",
        *(f"  - {change}" for change in res.changes),
        "",
        "Org id -> tenant mapping:",
        f"  EAP_ZITADEL_ORG_TENANT_MAP={res.org_id}={tenant}",
        "",
        "Add these fields to the deploy secrets (the secrets file you pass to up.sh), then deploy:",
        f"  EAP_ZITADEL_PROJECT_ID           {res.project_id}",
        f"  EAP_ZITADEL_ORG_TENANT_MAP       {res.org_id}={tenant}",
        f"  EAP_ZITADEL_STUDIO_CLIENT_ID     {res.studio_client_id}",
        "  EAP_ZITADEL_MACHINE_KEY          the machine user's key JSON (the same key this command used)",
    ]
    if access_team:
        lines += _access_setup(res, issuer=issuer, team=access_team)
    lines += ["", f"Bootstrap admin user id: {res.admin_user_id} (holds the admin role)"]
    return "\n".join(lines) + "\n"


def _access_setup(res: Result, *, issuer: str, team: str) -> list[str]:
    lines = [
        "",
        "Cloudflare Access identity provider (Zero Trust, generic OIDC):",
        f"  Issuer / endpoints               {issuer}  (auth URL {issuer}/oauth/v2/authorize, token URL {issuer}/oauth/v2/token,"
        f" certs URL {issuer}/oauth/v2/keys)",
        f"  Client ID                        {res.access_client_id}",
        f"  Redirect URL (in ZITADEL)        {access_config(team)['redirectUris'][0]}",
    ]
    if res.access_client_secret:
        return lines + [
            f"  Client secret                    {res.access_client_secret}",
            "  The client secret is shown only now. Store it as EAP_ZITADEL_ACCESS_CLIENT_SECRET in your secret store at once.",
        ]
    return lines + ["  Client secret                    (not shown: already issued; run again with --rotate-access-secret for a new one)"]


def _team(value: str) -> str:
    """The Zero Trust team name, from `<team>` or `<team>.cloudflareaccess.com`."""
    team = value.strip().lower().removesuffix(".cloudflareaccess.com")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", team):
        raise argparse.ArgumentTypeError("expected the team name or <team>.cloudflareaccess.com")
    return team


def _args(argv: list[str] | None, env: Mapping[str, str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m eap_iam_zitadel.bootstrap", description=__doc__.split("\n\n")[0])
    p.add_argument("--issuer", default=env.get("EAP_ZITADEL_ISSUER") or DEFAULT_ISSUER, help="the public issuer URL")
    p.add_argument("--api-url", default=env.get("EAP_ZITADEL_API_URL") or "",
                   help="where to reach ZITADEL (default: the issuer); with kubectl port-forward: http://127.0.0.1:8080")
    p.add_argument("--key-file", help="the machine user's key JSON (default: the environment variable EAP_ZITADEL_MACHINE_KEY)")
    p.add_argument("--tenant", default="studio", help="the platform tenant this organisation maps to")
    p.add_argument("--public-url", default=DEFAULT_PUBLIC_URL, help="Studio's public URL")
    p.add_argument("--loopback-url", default=DEFAULT_LOOPBACK_URL, help="Studio over the SSH tunnel")
    p.add_argument("--cloudflare-access", metavar="TEAM_DOMAIN", type=_team,
                   help="also create or repair the application Cloudflare Access signs in through, for this Zero Trust team "
                        "(<team> or <team>.cloudflareaccess.com), and print the Cloudflare setup")
    p.add_argument("--admin-user", default=env.get("ZITADEL_ADMIN_USERNAME") or "admin", help="the bootstrap admin's login name or e-mail")
    p.add_argument("--dry-run", action="store_true",
                   help="read only: call the API to see what exists, print what a real run would create, write nothing")
    p.add_argument("--rotate-access-secret", action="store_true", help="with --cloudflare-access: issue a new client secret for that application")
    p.add_argument("--platform-users", action="store_true",
                   help="create platform-provisioner and platform-members with their memberships; print each new key once")
    p.add_argument("--instance-policy", action="store_true",
                   help="no self-registration, no public organisation registration, a second factor required")
    p.add_argument("--no-force-mfa", action="store_true",
                   help="with --instance-policy: do not require a second factor (turns the requirement off: the rollback)")
    p.add_argument("--rotate-key", action="append", default=[], choices=sorted(platform_setup.PLATFORM_USERS),
                   help="with --platform-users: issue a new key for this user even though one exists (repeatable)")
    p.add_argument("--key-expires", default=None,
                   help="with --platform-users: expiry of a new key, RFC 3339 (default: ZITADEL's, no practical expiry)")
    args = p.parse_args(argv)
    if args.rotate_access_secret and not args.cloudflare_access:
        p.error("--rotate-access-secret needs --cloudflare-access")
    if args.cloudflare_access and (args.platform_users or args.instance_policy):
        p.error("--cloudflare-access cannot be combined with --platform-users or --instance-policy")
    if args.rotate_key and not args.platform_users:
        p.error("--rotate-key needs --platform-users")
    if args.no_force_mfa and not args.instance_policy:
        p.error("--no-force-mfa needs --instance-policy")
    return args


async def _platform(args: argparse.Namespace, client: ManagementClient, issuer: str, out: TextIO) -> None:
    def emit(text: str) -> None:
        out.write(text + "\n")
        out.flush()

    setup = platform_setup.PlatformSetup(
        client, dry_run=args.dry_run, emit=emit, force_mfa=not args.no_force_mfa,
        rotate=tuple(args.rotate_key), key_expires=args.key_expires,
    )
    try:
        if args.instance_policy:
            await setup.instance_policy()
        if args.platform_users:
            await setup.platform_users()
    except platform_setup.SetupError as exc:
        raise BootstrapError(str(exc)) from None
    out.write(platform_setup.render(setup, issuer=issuer, calls=client.trace))


async def amain(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    http: httpx2.AsyncClient | None = None,
) -> int:
    env = os.environ if env is None else env
    out = out or sys.stdout
    args = _args(argv, env)
    raw_key = ""
    if args.key_file:
        try:
            with open(args.key_file, encoding="utf-8") as handle:
                raw_key = handle.read()
        except OSError as exc:
            print(f"cannot read the key file: {type(exc).__name__}", file=sys.stderr)
            return 2
    raw_key = raw_key or env.get("EAP_ZITADEL_MACHINE_KEY", "")
    if not raw_key.strip():
        print("no machine-user key: pass --key-file or set EAP_ZITADEL_MACHINE_KEY", file=sys.stderr)
        return 2
    try:
        key = parse_machine_key(raw_key)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    issuer = args.issuer.rstrip("/")
    host = issuer.split("://", 1)[-1].split("/", 1)[0]
    client = ManagementClient(api_url=args.api_url or issuer, issuer=issuer, host=host, key=key, http=http)
    boot = Bootstrap(
        client, tenant=args.tenant, public_url=args.public_url, loopback_url=args.loopback_url,
        access_team=args.cloudflare_access, admin_user=args.admin_user, rotate_access_secret=args.rotate_access_secret, dry_run=args.dry_run,
    )
    platform = args.platform_users or args.instance_policy
    try:
        if platform:
            await _platform(args, client, issuer, out)
            return 0
        result = await boot.run()
    except (BootstrapError, ZitadelApiError) as exc:
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        if client.trace:  # what was reached before it failed
            print("calls made: " + ", ".join(f"{m} {p} -> {st or 'no answer'}" for m, p, st in client.trace), file=sys.stderr)
        return 1
    finally:
        if http is None:
            await client.aclose()
    if args.dry_run:
        out.write(render_dry_run(result, issuer=issuer, calls=client.trace))
        return 0
    out.write(render(result, issuer=issuer, tenant=args.tenant, access_team=args.cloudflare_access))
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
