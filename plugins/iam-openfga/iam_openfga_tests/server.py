"""A real OpenFGA for the integration tests: the plug-in's own and the API's Access tab test (which loads this file by
path, so it imports nothing from this test package).

Skipped unless a server is reachable: set `EAP_TEST_OPENFGA_URL` (and `EAP_TEST_OPENFGA_KEY`) to use one, or
`EAP_TEST_OPENFGA_DOCKER=1` to start the pinned image in a throw-away container (memory datastore; removed afterwards;
`EAP_TEST_DOCKER_CONTEXT` names the docker context, `EAP_TEST_OPENFGA_IMAGE` overrides the image)."""

import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid

import httpx2
import pytest

IMAGE = os.environ.get("EAP_TEST_OPENFGA_IMAGE", "openfga/openfga:v1.23.0")


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    # The docker context travels in the child's environment (DOCKER_CONTEXT), never as an argument, so the command
    # line is the fixed binary plus the arguments the tests build themselves.
    env = dict(os.environ)
    ctx = os.environ.get("EAP_TEST_DOCKER_CONTEXT")
    if ctx:
        env["DOCKER_CONTEXT"] = ctx
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120, check=check, env=env)


@pytest.fixture(scope="module")
def server():
    url = os.environ.get("EAP_TEST_OPENFGA_URL")
    if url:
        yield url.rstrip("/"), os.environ.get("EAP_TEST_OPENFGA_KEY") or None
        return
    if os.environ.get("EAP_TEST_OPENFGA_DOCKER") != "1":
        pytest.skip("no OpenFGA: set EAP_TEST_OPENFGA_URL, or EAP_TEST_OPENFGA_DOCKER=1 to start the pinned image")
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    key = secrets.token_urlsafe(24)
    name = f"eap-openfga-test-{uuid.uuid4().hex[:8]}"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    started = _docker(
        "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:8080",
        "-e", "OPENFGA_DATASTORE_ENGINE=memory", "-e", "OPENFGA_AUTHN_METHOD=preshared",
        "-e", f"OPENFGA_AUTHN_PRESHARED_KEYS={key}", "-e", "OPENFGA_PLAYGROUND_ENABLED=false", IMAGE, "run", check=False,
    )
    if started.returncode != 0:
        pytest.skip("the OpenFGA container did not start (is the image available?)")
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                if httpx2.get(url + "/healthz", timeout=2).status_code == 200:
                    break
            except httpx2.HTTPError:
                pass
            time.sleep(0.5)
        else:
            pytest.skip("the OpenFGA container never became healthy")
        yield url, key
    finally:
        _docker("rm", "-f", name, check=False)
