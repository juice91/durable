#!/usr/bin/env python3
"""End-to-end check of the Compose quick start (compose.yaml), as a new user does it. Standard library only.

    python3 scripts/ci/compose_smoke.py [--project NAME] [--port PORT] [--keep]

From fresh volumes: `docker compose up -d --build --wait`, take the setup code from `setup-link`, create the admin
through the API (as Studio's setup page does), sign in and call an authenticated route. Then `up` again and restart:
no secret is generated again and the admin is kept. Everything it started is removed with `down -v` (unless --keep).
It refuses a project that already has volumes, because the end of the run deletes them. No code, password or token is
printed."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMAND_TIMEOUT = 1800
SETUP_LINK = re.compile(r"^Open (\S+)/setup#code=([A-Z2-7-]{20,})\s*$", re.M)
USERNAME = "smoke-admin"


class Smoke:
    def __init__(self, project: str, port: int) -> None:
        self.project = project
        self.base = f"http://127.0.0.1:{port}"
        self.env = dict(os.environ, EAP_STUDIO_PORT=str(port))

    def run(self, args: list[str], *, quiet: bool = False) -> str:
        done = subprocess.run(
            args, cwd=ROOT, env=self.env, text=True, timeout=COMMAND_TIMEOUT,
            stdin=subprocess.DEVNULL,  # `compose run` can wait for an open stdin to close
            stdout=subprocess.PIPE, stderr=subprocess.PIPE if quiet else None,
        )
        if done.returncode != 0:
            raise SystemExit(f"command failed ({done.returncode}): {' '.join(args)}")
        return done.stdout

    def compose(self, *args: str, quiet: bool = False) -> str:
        return self.run(["docker", "compose", "-p", self.project, *args], quiet=quiet)

    def up(self, label: str, *extra: str) -> None:
        started = time.monotonic()
        self.compose("up", "-d", "--wait", "--wait-timeout", "600", *extra)
        print(f"TIMING {label}: {time.monotonic() - started:.0f}s", flush=True)

    def http(self, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
        """Through Studio's port: the same origin and proxy the browser uses."""
        request = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET")
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(request, data=data, timeout=60) as response:
                    status, raw = response.status, response.read()
            except urllib.error.HTTPError as exc:
                status, raw = exc.code, exc.read()
            except (urllib.error.URLError, ConnectionError) as exc:
                # A VM-backed Docker forwards a published port a moment after the container is healthy.
                if time.monotonic() >= deadline:
                    raise SystemExit(f"{self.base} is not reachable: {type(exc).__name__}") from None
                time.sleep(1)
                continue
            break
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, {}

    def secret_digests(self) -> str:
        """A digest of every secret file, to compare: never the values."""
        return self.in_init("cd /run/eap-secrets && sha256sum * | sort")

    def secret_modes(self) -> list[str]:
        """For each secrets volume: `mode uid:gid` of the directory, then each distinct one of its files."""
        found = self.in_init(
            "for d in /run/eap-secrets /var/lib/eap/postgres-secrets; do "
            "cd $d && stat -c '%a %u:%g' . && stat -c '%a %u:%g' * | sort -u; done"
        ).split("\n")
        return [line for line in found if line]

    def in_init(self, command: str) -> str:
        """As root in a throw-away `init` container, which mounts both volumes: each file has one reader."""
        return self.compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", "init", "-c", command, quiet=True)


def expect(condition: bool, what: str) -> None:
    if not condition:
        raise SystemExit(f"FAILED: {what}")
    print(f"ok: {what}", flush=True)


def sign_in(smoke: Smoke, password: str) -> str:
    status, session = smoke.http("/api/v1/auth/local/sign-in", {"username": USERNAME, "password": password})
    expect(status == 200 and bool(session.get("access_token")), f"sign in (HTTP {status})")
    return session["access_token"]


def check_signed_in(smoke: Smoke, token: str) -> None:
    status, capabilities = smoke.http("/api/v1/iam/capabilities", token=token)
    me = capabilities.get("me", {})
    expect(status == 200 and me.get("provider") == "localadmin", f"an authenticated call is the admin (HTTP {status})")
    status, _ = smoke.http("/api/v1/agents", token=token)
    expect(status == 200, f"list agents (HTTP {status})")


def scenario(smoke: Smoke) -> None:
    smoke.up("cold start (build and first up)", "--build")
    status, _ = smoke.http("/api/v1/iam/capabilities")
    expect(status == 401, "an unauthenticated call is refused")
    status, state = smoke.http("/api/v1/auth/local")
    expect(status == 200 and state.get("setup_required") is True, "setup is required on a fresh install")

    expect("setup code" in smoke.compose("logs", "api", quiet=True), "the API announced a setup code at start-up")
    answer = smoke.compose("exec", "-T", "api", "setup-link", quiet=True)
    found = SETUP_LINK.search(answer)
    expect(found is not None, "setup-link prints a link with a code")
    status, _ = smoke.http("/setup")
    expect(status == 200, "Studio serves the setup page")

    password = secrets.token_urlsafe(24)
    status, _ = smoke.http(
        "/api/v1/auth/local/setup", {"setup_code": found.group(2), "username": USERNAME, "password": password}
    )
    expect(status == 201, f"create the admin with the setup code (HTTP {status})")
    check_signed_in(smoke, sign_in(smoke, password))

    expect(
        smoke.secret_modes() == [
            "500 10001:10001", "400 0:0", "400 10001:10001",  # the platform's volume (root's: Postgres's originals)
            "500 999:999", "400 999:999",  # Postgres's volume
        ],
        "each secret is readable by its one user only",
    )
    before = smoke.secret_digests()
    smoke.up("second up (nothing to do)")
    smoke.compose("restart", "api", "worker", "studio")
    smoke.up("warm start after a restart")
    expect(smoke.secret_digests() == before, "no secret was generated again")
    smoke.compose("down")
    smoke.up("warm start (containers removed, volumes kept)")
    expect(smoke.secret_digests() == before, "the secrets survive `down`")
    check_signed_in(smoke, sign_in(smoke, password))
    answer = smoke.compose("exec", "-T", "api", "setup-link", quiet=True)
    expect("An admin account already exists" in answer, "setup-link says the admin exists")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default="eap-smoke", help="Compose project name (its volumes are deleted)")
    parser.add_argument("--port", type=int, default=3000, help="Studio's port on 127.0.0.1")
    parser.add_argument("--keep", action="store_true", help="leave the stack running")
    args = parser.parse_args()
    smoke = Smoke(args.project, args.port)
    existing = smoke.run(
        ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={args.project}"]
    ).strip()
    if existing:
        raise SystemExit(f"the project {args.project!r} already has volumes; remove them or pick another --project")
    try:
        scenario(smoke)
    except BaseException:
        subprocess.run(
            ["docker", "compose", "-p", args.project, "logs", "--tail", "80"], cwd=ROOT, env=smoke.env, check=False,
            stdout=sys.stderr, timeout=120,
        )
        raise
    finally:
        if not args.keep:
            smoke.compose("down", "-v", "--remove-orphans")
    print("compose quick start: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
