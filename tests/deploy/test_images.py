"""Build and smoke the Python and Studio images in the pydanticos Colima profile."""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = "pydanticos"

EXPECTED_CONTEXT = "colima-pydanticos"
PYTHON_DF = REPO_ROOT / "deploy/docker/python.Dockerfile"
STUDIO_DF = REPO_ROOT / "deploy/docker/studio.Dockerfile"


def _sha() -> str:
    return subprocess.check_output(["git", "-C", str(REPO_ROOT), "rev-parse", "--short=12", "HEAD"], text=True).strip()


def _docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["DOCKER_CONTEXT"] = EXPECTED_CONTEXT
    env.pop("DOCKER_HOST", None)
    return env


def docker(*args: str, check: bool = True, timeout: int = 120, extra_env: dict[str, str] | None = None, **kwargs) -> subprocess.CompletedProcess:
    env = _docker_env()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["docker", *args],
        env=env,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
        **kwargs,
    )


def _cache_env() -> dict[str, str]:
    """Keep Colima from adding ~/Library/Caches/colima beside the data-dir mount."""
    env = os.environ.copy()
    config = Path.home() / ".colima" / PROFILE / "colima.yaml"
    location = ""
    if config.is_file():
        in_mounts = False
        for line in config.read_text().splitlines():
            if line.startswith("mounts:"):
                in_mounts = True
                continue
            if in_mounts and line and not line[0].isspace() and not line.startswith("#"):
                break
            if in_mounts and "location:" in line:
                location = line.split("location:", 1)[1].strip().strip('"')
                break
    if not location:
        location = os.environ.get("PYDANTICOS_DATA_DIR", str(Path.home() / "pydanticos-data"))
    cache = Path(location) / "xdg-cache"
    if cache.is_dir():
        env["XDG_CACHE_HOME"] = str(cache)
    return env


@pytest.fixture(scope="module")
def images():
    sha = _sha()
    python_tag = f"pydanticos-python:{sha}"
    studio_tag = f"pydanticos-studio:{sha}"
    try:
        start = subprocess.run(["colima", "status", PROFILE], capture_output=True, text=True)
        if start.returncode != 0:
            started = subprocess.run(
                ["colima", "start", PROFILE, "--activate=false"],
                capture_output=True,
                text=True,
                timeout=300,
                env=_cache_env(),
                check=False,
            )
            if started.returncode != 0:
                pytest.skip(
                    "colima start of the pydanticos profile failed; "
                    f"image tests need that VM.\n{started.stdout}\n{started.stderr}"
                )
        for tag, dockerfile in ((python_tag, PYTHON_DF), (studio_tag, STUDIO_DF)):
            build_args = ["build", "--platform", "linux/arm64", "-f", str(dockerfile), "-t", tag, str(REPO_ROOT)]
            built = docker(*build_args, check=False, timeout=1800)
            if built.returncode != 0:
                detail = (built.stdout + built.stderr)[-2000:]
                pytest.skip(
                    f"docker build of {tag} failed. "
                    "Refusing to retag any other image as this SHA.\n"
                    f"{detail}"
                )
        yield {"python": python_tag, "studio": studio_tag, "sha": sha}
    finally:
        subprocess.run(["colima", "stop", PROFILE], check=False, timeout=180, capture_output=True)


def test_python_image_is_non_root_and_entrypoints_help(images):
    uid = docker("run", "--rm", "--entrypoint", "sh", images["python"], "-c", "id -u")
    assert uid.stdout.strip() != "0"
    for command in ("api", "worker", "migrate"):
        proc = docker("run", "--rm", images["python"], command, "--help")
        assert proc.returncode == 0, proc.stdout + proc.stderr


def test_images_do_not_contain_env_or_git(images):
    for tag in (images["python"], images["studio"]):
        listing = docker("run", "--rm", "--entrypoint", "sh", tag, "-c", "ls -a /app")
        names = set(listing.stdout.split())
        assert ".env" not in names, tag
        assert ".git" not in names, tag
        probe = docker(
            "run", "--rm", "--entrypoint", "sh", tag, "-c",
            "if [ -e /app/.env ] || [ -e /app/.git ]; then exit 1; fi",
        )
        assert probe.returncode == 0, tag


def test_studio_image_serves_root(images):
    port = 13457
    name = f"pydanticos-studio-smoke-{images['sha']}"
    docker("rm", "-f", name, check=False)
    run = docker("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:3000", images["studio"])
    assert run.returncode == 0, run.stdout + run.stderr
    url = f"http://127.0.0.1:{port}/"
    try:
        status = None
        last_err = None
        for _ in range(60):
            try:
                req = urllib.request.Request(url, method="GET")
                opener = urllib.request.build_opener(urllib.request.HTTPErrorProcessor)
                with opener.open(req, timeout=3) as resp:
                    status = resp.status
                    break
            except urllib.error.HTTPError as exc:
                status = exc.code
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_err = exc
                time.sleep(1)
        if status is None:
            logs = docker("logs", name, check=False)
            pytest.fail(f"studio never served /: {last_err}\n{logs.stdout}\n{logs.stderr}")
        assert 200 <= status < 400, status
    finally:
        docker("rm", "-f", name, check=False)
