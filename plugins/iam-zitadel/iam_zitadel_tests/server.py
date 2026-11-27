"""A real ZITADEL v4.20.5 with Postgres for the contract tests, the version the deployment pins.

Skipped unless `EAP_TEST_ZITADEL_DOCKER=1`: it starts two throw-away containers on a throw-away network (removed
afterwards, volumes included) and takes about a minute. `EAP_TEST_DOCKER_CONTEXT` names the docker context,
`EAP_TEST_ZITADEL_IMAGE` and `EAP_TEST_ZITADEL_PG_IMAGE` override the images, `EAP_TEST_ZITADEL_NAME` the name
prefix (CI sets it, so a cancelled job can be cleaned up). Every credential is generated per run and
stays in memory or in the throw-away containers. A skip is not a pass."""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import string
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx2
import pytest

IMAGE = os.environ.get(
    "EAP_TEST_ZITADEL_IMAGE",  # the digest deploy/k8s/iam/zitadel.yaml pins
    "ghcr.io/zitadel/zitadel:v4.20.5@sha256:cb21fe73badfe44972bb24a66cf0ebce6a8dc63ab8ed24567af6b731bd8bbf45",
)
PG_IMAGE = os.environ.get("EAP_TEST_ZITADEL_PG_IMAGE", "postgres:17")
START_SECONDS = 240


@dataclass(frozen=True)
class RealZitadel:
    url: str  # also the issuer: the instance's external domain is `localhost`
    key_json: str  # the first instance's machine user (instance owner), like `platform-automation`
    admin_password: str


def _env() -> dict[str, str]:
    # The docker context travels in the child's environment, never as an argument.
    ctx = os.environ.get("EAP_TEST_DOCKER_CONTEXT")
    return {**os.environ, **({"DOCKER_CONTEXT": ctx} if ctx else {})}


def _docker(*args: str, check: bool = True, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check, env=_env())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def zitadel() -> Iterator[RealZitadel]:
    if os.environ.get("EAP_TEST_ZITADEL_DOCKER") != "1":
        pytest.skip("no ZITADEL: set EAP_TEST_ZITADEL_DOCKER=1 to start the pinned image")
    if shutil.which("docker") is None:
        pytest.fail("EAP_TEST_ZITADEL_DOCKER=1 but docker is not installed")
    name = os.environ.get("EAP_TEST_ZITADEL_NAME") or f"eap-zitadel-test-{uuid.uuid4().hex[:8]}"
    network, database, server = (f"{name}{suffix}" for suffix in ("-net", "-pg", "-idp"))
    port = _free_port()
    db_password = secrets.token_hex(16)
    alphabet = string.ascii_letters + string.digits
    master_key = "".join(secrets.choice(alphabet) for _ in range(32))
    admin_password = "Aa1!" + secrets.token_hex(12)
    try:
        _docker("network", "create", network)
        _docker("run", "-d", "--name", database, "--network", network, "-e", f"POSTGRES_PASSWORD={db_password}", PG_IMAGE)
        for _ in range(90):
            # -h: the init-time server listens on the socket only, so a TCP answer means the real one is up.
            if _docker("exec", database, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("the ZITADEL test database never became ready")
        config = {
            "ZITADEL_MASTERKEY": master_key,
            "ZITADEL_DATABASE_POSTGRES_HOST": database,
            "ZITADEL_DATABASE_POSTGRES_PORT": "5432",
            "ZITADEL_DATABASE_POSTGRES_DATABASE": "zitadel",
            "ZITADEL_DATABASE_POSTGRES_USER_USERNAME": "zitadel",
            "ZITADEL_DATABASE_POSTGRES_USER_PASSWORD": db_password,
            "ZITADEL_DATABASE_POSTGRES_USER_SSL_MODE": "disable",
            "ZITADEL_DATABASE_POSTGRES_ADMIN_USERNAME": "postgres",
            "ZITADEL_DATABASE_POSTGRES_ADMIN_PASSWORD": db_password,
            "ZITADEL_DATABASE_POSTGRES_ADMIN_SSL_MODE": "disable",
            "ZITADEL_EXTERNALDOMAIN": "localhost",
            "ZITADEL_EXTERNALPORT": str(port),
            "ZITADEL_EXTERNALSECURE": "false",
            "ZITADEL_DEFAULTINSTANCE_FEATURES_LOGINV2_REQUIRED": "false",
            "ZITADEL_FIRSTINSTANCE_ORG_NAME": "Durable",
            "ZITADEL_FIRSTINSTANCE_ORG_HUMAN_PASSWORD": admin_password,
            "ZITADEL_FIRSTINSTANCE_ORG_MACHINE_MACHINE_USERNAME": "platform-automation",
            "ZITADEL_FIRSTINSTANCE_ORG_MACHINE_MACHINE_NAME": "Platform automation",
            "ZITADEL_FIRSTINSTANCE_ORG_MACHINE_MACHINEKEY_TYPE": "1",
            "ZITADEL_FIRSTINSTANCE_MACHINEKEYPATH": "/machine-key.json",
        }
        env_args = [part for name, value in config.items() for part in ("-e", f"{name}={value}")]
        _docker(
            # Root, in this throw-away container only: the distroless image has no directory its own user can write the
            # first instance's key file to.
            "run", "-d", "--user", "0", "--name", server, "--network", network, "-p", f"127.0.0.1:{port}:8080", *env_args, IMAGE,
            "start-from-init", "--masterkeyFromEnv", "--tlsMode", "disabled",
        )
        url = f"http://localhost:{port}"
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            try:
                # `/debug/ready` answers before the REST gateway can reach its own gRPC server: ask an API too.
                if all(
                    httpx2.get(url + path, timeout=3).status_code == 200
                    for path in ("/debug/ready", "/management/v1/healthz", "/admin/v1/healthz", "/auth/v1/healthz")
                ):
                    break
            except httpx2.HTTPError:
                pass
            time.sleep(1)
        else:
            logs = _docker("logs", "--tail", "30", server, check=False)
            pytest.fail(f"ZITADEL never became ready:\n{(logs.stdout + logs.stderr)[-3000:]}")
        # The image is distroless (no `cat`): copy the key file out.
        copied = subprocess.run(
            ["docker", "cp", f"{server}:/machine-key.json", "-"], capture_output=True, timeout=60, check=True, env=_env()
        )
        yield RealZitadel(url=url, key_json=_untar_single(copied.stdout), admin_password=admin_password)
    finally:
        _docker("rm", "-f", "-v", server, database, check=False)
        _docker("network", "rm", network, check=False)


def _untar_single(data: bytes) -> str:
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        handle = tar.extractfile(tar.getmembers()[0])
        assert handle is not None
        return handle.read().decode()
