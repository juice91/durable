"""deploy/scripts/local-admin.sh (and its use by scripts/dev-lib.sh): the signing key is generated only where there is
none, never printed and never written into the repository; the deploy ends with the block that creates the admin.

The functions run here with a stubbed cluster. No key or code in this file is a literal: they are made at run time."""

from __future__ import annotations

import base64
import os
import re
import secrets
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "deploy" / "scripts" / "local-admin.sh"
NAME = "EAP_LOCAL_AUTH_SIGNING_KEY"
CODE = "-".join(["ABCD", "EFGH"] * 4)

PRELUDE = f"""
set -euo pipefail
NAMESPACE=pydanticos
EXPECTED_CONTEXT=test-context
LOCAL_ADMIN_RETRY_SECONDS=0
dotenv_get() {{ awk -v k="$2" 'index($0, k "=") == 1 {{print substr($0, length(k) + 2); exit}}' "$1"; }}
kubectl_for_profile() {{
  printf '%s\\n' "$*" >>"$STUB_LOG"
  case "$*" in
    *"get secret"*) [[ -z "${{STUB_READ_FAILS:-}}" ]] || return 1; printf '%s' "${{STUB_SECRET:-}}" ;;
    *"setup-code"*)
      [[ -z "${{STUB_EXEC_FAILS:-}}" ]] || return 1
      [[ -z "${{STUB_ADMIN_EXISTS:-}}" ]] || return 3
      printf 'eap localadmin setup code: %s  (no admin yet)\\n' "$STUB_CODE" ;;
  esac
}}
source "{LIBRARY}"
"""


def run(tmp: Path, script: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", PRELUDE + script], capture_output=True, text=True, timeout=60,
        env={**os.environ, "STUB_LOG": str(tmp / "kubectl.log"), **env},
    )


def log(tmp: Path) -> str:
    path = tmp / "kubectl.log"
    return path.read_text() if path.exists() else ""


def secrets_file(tmp: Path, text: str) -> Path:
    path = tmp / "secrets.env"
    path.write_text(text)
    path.chmod(0o600)
    return path


def values(path: Path) -> list[str]:
    return [line.split("=", 1)[1] for line in path.read_text().splitlines() if line.startswith(NAME + "=")]


def ensure(tmp: Path, path: Path, **env: str) -> subprocess.CompletedProcess:
    return run(tmp, f'ensure_cluster_signing_key "{path}"', **env)


def test_a_key_in_the_secrets_input_is_used_and_the_cluster_is_not_read(tmp_path):
    key = secrets.token_hex(32)
    path = secrets_file(tmp_path, f"A=1\n{NAME}={key}\n")
    done = ensure(tmp_path, path)
    assert done.returncode == 0, done.stderr
    assert values(path) == [key] and log(tmp_path) == "" and key not in done.stdout + done.stderr


def test_the_secrets_existing_value_is_kept(tmp_path):
    key = secrets.token_hex(32)
    path = secrets_file(tmp_path, f"A=1\n{NAME}=\nB=2\n")
    done = ensure(tmp_path, path, STUB_SECRET=base64.b64encode(key.encode()).decode())
    assert done.returncode == 0, done.stderr
    assert values(path) == [key] and path.read_text().startswith("A=1\nB=2\n")
    assert "--ignore-not-found" in log(tmp_path) and key not in done.stdout + done.stderr + log(tmp_path)


def test_a_key_is_generated_only_when_there_is_none_and_never_printed(tmp_path):
    made = []
    for n in range(2):
        path = secrets_file(tmp_path, "A=1\n" if n else f"A=1\n{NAME}=\n")
        done = ensure(tmp_path, path)
        assert done.returncode == 0, done.stderr
        (key,) = values(path)
        assert re.fullmatch(r"[0-9a-f]{64}", key)
        assert key not in done.stdout + done.stderr + log(tmp_path) and "Generated" in done.stdout
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert sorted(p.name for p in tmp_path.iterdir()) == ["kubectl.log", "secrets.env"]  # no other copy
        made.append(key)
    assert made[0] != made[1]


def test_a_secret_that_cannot_be_read_is_never_overwritten(tmp_path):
    path = secrets_file(tmp_path, f"{NAME}=\n")
    done = ensure(tmp_path, path, STUB_READ_FAILS="1")
    assert done.returncode != 0 and values(path) == [""]
    assert "pydanticos-secrets" in done.stderr and "again" in done.stderr


def setup_block(tmp: Path, **env: str) -> str:
    done = run(tmp, "print_cluster_setup", **{"STUB_CODE": CODE, **env})
    assert done.returncode == 0, done.stderr
    return done.stdout


AGAIN = "kubectl --context test-context -n pydanticos exec deploy/api -- python -m eap_iam_localadmin setup-code"


def test_the_block_gives_the_url_the_code_and_how_to_get_a_new_one(tmp_path):
    out = setup_block(tmp_path)
    assert "Create your admin account" in out
    assert f"http://127.0.0.1:13000/setup#code={CODE}" in out and f"Setup code (if the page asks): {CODE}" in out
    assert "port-forward svc/studio 13000:3000" in out and "port-forward svc/api 18000:8000" in out
    assert AGAIN in out


def test_a_published_studio_url_needs_no_port_forward(tmp_path):
    out = setup_block(tmp_path, PYDANTICOS_STUDIO_URL="https://studio.example.com/")
    assert f"https://studio.example.com/setup#code={CODE}" in out and "port-forward" not in out


def test_with_an_admin_there_is_no_code(tmp_path):
    out = setup_block(tmp_path, STUB_ADMIN_EXISTS="1")
    assert "admin account exists" in out and CODE not in out and "setup#code" not in out
    assert "reset-password" in out


def test_a_deploy_still_succeeds_when_the_code_cannot_be_read_and_says_what_to_run(tmp_path):
    out = setup_block(tmp_path, STUB_EXEC_FAILS="1")
    assert AGAIN in out and "setup#code" not in out
    assert log(tmp_path).count("setup-code") > 1  # it waited for the API


def dev(tmp: Path, **env: str) -> subprocess.CompletedProcess:
    script = f"""
set -euo pipefail
source "{ROOT}/scripts/dev-lib.sh"
DEV_DIR="{tmp}/.dev"
ensure_local_admin_env
printf '%s|%s|%s' "${{{NAME}:-}}" "${{EAP_STUDIO_AUTH:-}}" "${{EAP_IAM_MEMBERSHIP:-}}" >"{tmp}/env"
"""
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("EAP_LOCAL", "EAP_IAM", "EAP_STUDIO"))}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env={**clean, **env})


def test_dev_up_generates_a_key_outside_git_once(tmp_path):
    listed = {"EAP_IAM_IDENTITY": "localadmin,apikey"}
    first = dev(tmp_path, **listed)
    assert first.returncode == 0, first.stderr
    key, studio, membership = (tmp_path / "env").read_text().split("|")
    stored = tmp_path / ".dev" / "local-auth-signing-key"
    assert re.fullmatch(r"[0-9a-f]{64}", key) and stored.read_text().strip() == key
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert (studio, membership) == ("local", "localadmin") and key not in first.stdout + first.stderr
    assert dev(tmp_path, **listed).returncode == 0
    assert (tmp_path / "env").read_text().startswith(key + "|")  # the same key: sessions survive a restart
    assert ".dev/" in (ROOT / ".gitignore").read_text().split()


def studio_bearer(**env: str) -> str:
    """What dev-up hands Studio as its loopback dev bearer (`unset` when nothing)."""
    script = f"""
set -euo pipefail
source "{ROOT}/scripts/dev-lib.sh"
ensure_env
ensure_studio_dev_env
printf '%s' "${{EAP_STUDIO_LOOPBACK_DEV_BEARER-unset}}"
"""
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("EAP_", "DBOS_", "POSTGRES_"))}
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env={**clean, **env})
    assert done.returncode == 0, done.stderr
    return done.stdout


# The dev bearer: no credential, the API accepts it from a loopback peer in dev mode only (docs/iam.md).
DEV_BEARER = " ".join(["Bearer", "dev:acme:alice"])


@pytest.mark.parametrize("env", [{}, {"EAP_IAM_IDENTITY": "apikey,devloopback"}, {"EAP_STUDIO_AUTH": ""}])
def test_dev_up_gives_a_default_studio_the_loopback_dev_bearer(env):
    """A clean checkout (the public tree carries no apps/studio/.env.development) needs no sign-in method."""
    assert studio_bearer(**env) == DEV_BEARER


@pytest.mark.parametrize(
    "env",
    [
        {"EAP_STUDIO_AUTH": "local"},
        {"EAP_STUDIO_AUTH": "zitadel"},
        {"EAP_AUTH_MODE": "api_key"},
        {"EAP_AUTH_MODE": "production"},
        {"EAP_IAM_IDENTITY": "apikey"},
        # The built-in admin in development: Studio signs in with it (the bearer would win over the form).
        {"EAP_IAM_IDENTITY": "localadmin,apikey,devloopback", "EAP_STUDIO_AUTH": "local"},
    ],
)
def test_dev_up_leaves_a_configured_sign_in_or_a_closed_api_alone(env):
    assert studio_bearer(**env) == "unset"


@pytest.mark.parametrize("own", ["Bearer dev:other:bob", ""])
def test_dev_up_keeps_a_studio_bearer_that_is_set_and_an_empty_one_turns_it_off(own):
    assert studio_bearer(EAP_STUDIO_LOOPBACK_DEV_BEARER=own) == own


def test_dev_up_applies_the_studio_bearer_after_the_local_admin_settings():
    lines = (ROOT / "scripts" / "dev-up.sh").read_text().splitlines()
    assert lines.index("ensure_studio_dev_env") > lines.index("ensure_local_admin_env")


def test_dev_up_keeps_a_key_that_is_set_and_does_nothing_without_the_provider(tmp_path):
    own = secrets.token_hex(32)
    assert dev(tmp_path, EAP_IAM_IDENTITY="localadmin", **{NAME: own}).returncode == 0
    assert (tmp_path / "env").read_text().startswith(own + "|") and not (tmp_path / ".dev").exists()
    assert dev(tmp_path, EAP_IAM_IDENTITY="apikey,devloopback").returncode == 0
    assert (tmp_path / "env").read_text() == "||" and not (tmp_path / ".dev").exists()


def test_up_sh_generates_the_key_and_ends_with_the_setup_block(tmp_path):
    """The whole public deploy script against stubbed tools."""
    from test_scripts import SECRET_KEYS, SECRET, _env, _install_stubs, _run

    path = secrets_file(tmp_path, "".join(f"{key}={SECRET}\n" for key in SECRET_KEYS) + f"{NAME}=\n")
    env = _env(tmp_path, _install_stubs(tmp_path), {"STUB_SETUP_CODE": CODE})
    done = _run("up.sh", env, ["--secrets-env", str(path)])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Generated the sign-in signing key" in done.stdout
    assert f"http://127.0.0.1:13000/setup#code={CODE}" in done.stdout
    assert done.stdout.index("Deployed ") < done.stdout.index("Create your admin account")
    assert values(path) == [""]  # the operator's own file is not changed
