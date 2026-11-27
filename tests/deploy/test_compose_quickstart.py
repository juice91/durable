"""The Compose quick start (compose.yaml, docs/quickstart-compose.md): shape, generated secrets, `*_FILE` settings and
the setup link. No Docker needed; scripts/ci/compose_smoke.py runs the stack itself."""

from __future__ import annotations

import base64
import importlib.util
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = ROOT / "deploy" / "docker"
ENTRYPOINT = DOCKER_DIR / "python-entrypoint.sh"
PYTHON_SERVICES = ("init", "migrate", "api", "worker")
SECRET_FILES = (
    "postgres_password", "app_db_password", "dbos_db_password", "database_url", "dbos_url", "secrets_key",
    "audit_hash_salt", "local_auth_signing_key",
)
SECRET_NAME = re.compile(r"(KEY|PASSWORD|SALT|TOKEN)(S)?$")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


def _volumes(service: dict[str, Any]) -> dict[str, str]:
    """Mount target to the whole `source:target[:mode]` entry."""
    return {entry.split(":")[1]: entry for entry in service.get("volumes", [])}


# ---- compose.yaml -------------------------------------------------------------------------------------------------


def test_only_studio_and_grafana_are_published_and_only_on_loopback(compose):
    published = {name: s["ports"] for name, s in compose["services"].items() if s.get("ports")}
    assert set(published) == {"studio", "lgtm"}
    for ports in published.values():
        assert all(port.startswith("127.0.0.1:") for port in ports), ports


def test_observability_is_an_optional_profile(compose):
    profiled = {name for name, s in compose["services"].items() if s.get("profiles")}
    assert profiled == {"lgtm"}
    assert compose["services"]["lgtm"]["profiles"] == ["observability"]


def test_services_start_in_order_and_long_running_ones_report_health(compose):
    services = compose["services"]
    needs = {name: {dep: v["condition"] for dep, v in s.get("depends_on", {}).items()} for name, s in services.items()}
    assert needs["postgres"] == {"init": "service_completed_successfully"}
    assert needs["migrate"] == {"init": "service_completed_successfully", "postgres": "service_healthy"}
    assert needs["api"] == {"migrate": "service_completed_successfully"}
    assert needs["worker"] == {"migrate": "service_completed_successfully", "api": "service_healthy"}
    assert needs["studio"] == {"api": "service_healthy"}
    for name in ("postgres", "api", "worker", "studio"):
        assert services[name]["healthcheck"]["test"], name


def test_images_are_built_from_the_repository_dockerfiles_and_never_pulled(compose):
    services = compose["services"]
    builds = {name: s["build"]["dockerfile"] for name, s in services.items() if "build" in s}
    assert builds == {"init": "deploy/docker/python.Dockerfile", "studio": "deploy/docker/studio.Dockerfile"}
    assert {services[name]["image"] for name in PYTHON_SERVICES} == {"eap-python:${EAP_IMAGE_TAG:-local}"}
    for name in ("migrate", "api", "worker"):
        assert services[name]["pull_policy"] == "never", name
    # Studio's server forwards /api/ to the API service: one origin, the API unpublished.
    assert services["studio"]["build"]["args"]["EAP_API_INTERNAL_URL"] == "http://api:8000"


def test_secrets_reach_only_the_platform_and_postgres_and_only_init_writes_them(compose):
    mounts = {
        name: _volumes(s)["/run/eap-secrets"]
        for name, s in compose["services"].items() if "/run/eap-secrets" in _volumes(s)
    }
    assert set(mounts) == {"init", "postgres", "migrate", "api", "worker"}
    assert [name for name, entry in mounts.items() if not entry.endswith(":ro")] == ["init"]
    assert compose["services"]["init"]["network_mode"] == "none"
    # Postgres sees its own volume only, which nothing else but `init` mounts.
    assert mounts.pop("postgres") == "postgres_secrets:/run/eap-secrets:ro"
    assert all(entry.startswith("secrets:") for entry in mounts.values()), mounts
    assert _volumes(compose["services"]["init"])["/var/lib/eap/postgres-secrets"].startswith("postgres_secrets:")
    users = [name for name, s in compose["services"].items()
             if any(entry.startswith("postgres_secrets:") for entry in s.get("volumes", []))]
    assert users == ["init", "postgres"]
    assert compose["services"]["init"]["command"] == [
        "/run/eap-secrets", "/var/lib/eap/blobs", "/var/lib/eap/initdb", "/var/lib/eap/postgres-secrets",
    ]


def test_the_stack_mounts_named_volumes_only(compose):
    """No bind mount: it works wherever the Docker engine is, whether or not it can see the checkout."""
    for name, service in compose["services"].items():
        for entry in service.get("volumes", []):
            assert entry.split(":")[0] in compose["volumes"], f"{name}: {entry}"
    assert _volumes(compose["services"]["init"])["/var/lib/eap/initdb"].startswith("postgres_init:")
    assert _volumes(compose["services"]["postgres"])["/docker-entrypoint-initdb.d"] == (
        "postgres_init:/docker-entrypoint-initdb.d:ro"
    )


def test_compose_holds_no_secret_value(compose):
    """A setting that carries a secret is a path (`_FILE`) or a name passed through from the user's .env."""
    for name, service in compose["services"].items():
        for setting, value in (service.get("environment") or {}).items():
            if SECRET_NAME.search(setting):
                assert value is None, f"{name}: {setting}"


def test_every_file_setting_names_a_generated_secret(compose):
    for name, service in compose["services"].items():
        for setting, value in (service.get("environment") or {}).items():
            if setting.endswith("_FILE"):
                directory, file_name = value.rsplit("/", 1)
                assert directory == "/run/eap-secrets" and file_name in SECRET_FILES, f"{name}: {setting}"


def test_the_defaults_are_one_local_admin_and_nothing_external(compose):
    services = compose["services"]
    for name in ("migrate", "api", "worker"):
        env = services[name]["environment"]
        assert env["EAP_IAM_IDENTITY"] == "localadmin,apikey", name
        assert env["EAP_IAM_MEMBERSHIP"] == "localadmin", name
        assert env["EAP_IAM_POLICY"] == "openaccess", name
    api = services["api"]["environment"]
    assert api["EAP_AUTH_MODE"] == "production"
    assert api["EAP_WEBHOOKS_ENABLED"] == "false" and api["EAP_EXTERNAL_ACCESS_ENABLED"] == "false"
    assert services["studio"]["environment"] == {"EAP_STUDIO_AUTH": "local"}
    text = (ROOT / "compose.yaml").read_text().lower()
    for absent in ("cloudflare", "zitadel", "openfga", "devloopback"):
        assert absent not in text, absent


def test_api_and_worker_share_the_blob_volume(compose):
    for name in ("api", "worker"):
        service = compose["services"][name]
        assert _volumes(service)[service["environment"]["EAP_BLOB_DIR"]].startswith("blobs:")


def test_every_setting_in_the_example_file_is_commented_out():
    lines = (ROOT / ".env.example").read_text().splitlines()
    assert [line for line in lines if line.strip() and not line.startswith("#")] == []
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "EAP_LITELLM_BASE_URL"):
        assert any(line.startswith(f"# {name}=") for line in lines), name


def test_provider_settings_of_the_example_file_reach_the_platform(compose):
    """A setting the example offers must be passed into the API and the worker (or be Compose's own)."""
    offered = set(re.findall(r"^# ([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    development = {"POSTGRES_USER", "POSTGRES_PASSWORD", "EAP_DATABASE_URL", "DBOS_SYSTEM_DATABASE_URL"}
    composes_own = {"EAP_STUDIO_PORT", "COMPOSE_PROFILES", "EAP_GRAFANA_PORT"}
    for name in ("api", "worker"):
        missing = offered - development - composes_own - set(compose["services"][name]["environment"])
        assert missing == set(), (name, missing)


def test_the_python_image_carries_the_quick_start_scripts():
    text = (DOCKER_DIR / "python.Dockerfile").read_text()
    assert "COPY deploy/docker/compose-init.py deploy/docker/compose-postgres-init.sh /usr/local/lib/eap/" in text
    assert "COPY deploy/docker/setup-link.sh /usr/local/bin/setup-link" in text
    assert "install -d -o eap -g eap /var/lib/eap/blobs" in text


def test_the_quick_start_scripts_pass_shellcheck():
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed")
    scripts = [str(p) for p in sorted(DOCKER_DIR.glob("*.sh"))]
    subprocess.run(["shellcheck", *scripts], check=True, timeout=60)


# ---- the init service ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def init_module():
    spec = importlib.util.spec_from_file_location("compose_init", DOCKER_DIR / "compose-init.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _contents(directory: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in directory.iterdir()}


def test_init_generates_each_secret_once_and_prints_no_value(init_module, tmp_path, capsys):
    directory = tmp_path / "secrets"
    assert init_module.main([str(directory), str(tmp_path / "blobs")]) == 0
    first = _contents(directory)
    assert set(first) == set(SECRET_FILES)
    assert (tmp_path / "blobs").is_dir()
    assert init_module.main([str(directory)]) == 0
    assert _contents(directory) == first
    output = capsys.readouterr()
    assert "nothing generated" in output.out
    for value in first.values():
        assert value not in output.out + output.err
    for name in SECRET_FILES:
        assert name not in output.out + output.err
    assert str(directory) not in output.out + output.err
    assert f"generated {len(SECRET_FILES)} secrets" in output.out
    assert stat.S_IMODE(directory.stat().st_mode) == 0o500
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o400, path.name


# Which files Postgres (and its first-start script) reads, as 999; the platform's containers read the rest as 10001.
POSTGRES_READS = {"postgres_password", "app_db_password", "dbos_db_password"}


def _run_init(init_module, tmp_path: Path) -> tuple[Path, Path]:
    platform, postgres = tmp_path / "secrets", tmp_path / "postgres-secrets"
    arguments = [platform, tmp_path / "blobs", tmp_path / "initdb", postgres]
    assert init_module.main([str(argument) for argument in arguments]) == 0
    return platform, postgres


def test_init_gives_postgres_a_volume_of_its_own_with_only_what_it_reads(init_module, tmp_path):
    platform, postgres = _run_init(init_module, tmp_path)
    assert _contents(postgres) == {name: _contents(platform)[name] for name in POSTGRES_READS}
    for directory in (platform, postgres):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o500
        assert {stat.S_IMODE(p.stat().st_mode) for p in directory.iterdir()} == {0o400}


def test_init_as_root_gives_each_file_and_directory_to_one_user(init_module, tmp_path, monkeypatch):
    owners: dict[str, tuple[int, int]] = {}

    def chown(path, uid, gid):
        owners[str(Path(path).relative_to(tmp_path))] = (uid, gid)

    monkeypatch.setattr(init_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(init_module.os, "chown", chown)
    for _ in range(2):  # applied again to files that exist
        owners.clear()
        _run_init(init_module, tmp_path)
        platform = {f"secrets/{n}": (0, 0) if n in POSTGRES_READS else (10001, 10001) for n in SECRET_FILES}
        postgres = {f"postgres-secrets/{n}": (999, 999) for n in POSTGRES_READS}
        assert owners == {
            "blobs": (10001, 10001), "secrets": (10001, 10001), "postgres-secrets": (999, 999), **platform, **postgres,
        }


def test_every_secret_a_service_is_given_is_one_its_user_may_read(compose, init_module):
    assert init_module.POSTGRES_FILES == POSTGRES_READS
    for name in ("postgres", "migrate", "api", "worker"):
        for setting, value in compose["services"][name]["environment"].items():
            if setting.endswith("_FILE"):
                assert (value.rsplit("/", 1)[1] in POSTGRES_READS) == (name == "postgres"), f"{name}: {setting}"
    script = (DOCKER_DIR / "compose-postgres-init.sh").read_text()
    read = set(re.findall(r"\$\{secrets\}/([a-z_]+)", script))
    assert read and read <= POSTGRES_READS


def test_init_narrows_volumes_made_with_wider_modes_and_keeps_their_values(init_module, tmp_path):
    platform, postgres = _run_init(init_module, tmp_path)
    before = _contents(platform)
    for directory in (platform, postgres):
        directory.chmod(0o755)
        for path in directory.iterdir():
            path.chmod(0o444)
    _run_init(init_module, tmp_path)
    assert _contents(platform) == before
    for directory in (platform, postgres):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o500
        assert {stat.S_IMODE(p.stat().st_mode) for p in directory.iterdir()} == {0o400}


def test_init_brings_postgres_s_copy_back_in_step(init_module, tmp_path):
    platform, postgres = _run_init(init_module, tmp_path)
    postgres.chmod(0o700)
    (postgres / "app_db_password").unlink()
    (postgres / "app_db_password").write_text("stale")
    _run_init(init_module, tmp_path)
    assert (postgres / "app_db_password").read_text() == (platform / "app_db_password").read_text()


def test_init_gives_postgres_its_first_start_script_every_time(init_module, tmp_path):
    initdb = tmp_path / "initdb"
    for _ in range(2):  # an upgrade replaces it
        assert init_module.main([str(tmp_path / "s"), str(tmp_path / "b"), str(initdb)]) == 0
    script = initdb / "01-setup-databases.sh"
    assert script.read_bytes() == (DOCKER_DIR / "compose-postgres-init.sh").read_bytes()
    assert stat.S_IMODE(script.stat().st_mode) == 0o555


def test_init_values_have_the_shape_the_platform_needs(init_module, tmp_path):
    init_module.fill(tmp_path)
    values = _contents(tmp_path)
    assert len(base64.b64decode(values["secrets_key"], validate=True)) == 32
    assert values["database_url"] == (
        f"postgresql+psycopg://eap_app:{values['app_db_password']}@postgres:5432/eap_app"
    )
    assert values["dbos_url"] == f"postgresql://eap_dbos:{values['dbos_db_password']}@postgres:5432/eap_dbos"
    for name in ("postgres_password", "app_db_password", "dbos_db_password", "audit_hash_salt", "local_auth_signing_key"):
        assert re.fullmatch(r"[0-9a-f]{64}", values[name]), name
    assert len(set(values.values())) == len(values)


def test_init_s_sign_in_key_is_accepted_by_the_plug_in(init_module, tmp_path):
    from eap_iam_localadmin.settings import load_settings

    init_module.fill(tmp_path)
    settings = load_settings({"EAP_LOCAL_AUTH_SIGNING_KEY_FILE": str(tmp_path / "local_auth_signing_key")})
    assert len(settings.signing_key) == 64


def test_init_differs_between_installs(init_module, tmp_path):
    init_module.fill(tmp_path / "a")
    init_module.fill(tmp_path / "b")
    first, second = _contents(tmp_path / "a"), _contents(tmp_path / "b")
    assert all(first[name] != second[name] for name in SECRET_FILES if not name.endswith("url"))


def test_init_adds_only_what_is_missing(init_module, tmp_path):
    init_module.fill(tmp_path)
    before = _contents(tmp_path)
    tmp_path.chmod(0o700)
    (tmp_path / "audit_hash_salt").unlink()
    assert init_module.fill(tmp_path) == 1
    after = _contents(tmp_path)
    assert {k: v for k, v in after.items() if k != "audit_hash_salt"} == {
        k: v for k, v in before.items() if k != "audit_hash_salt"
    }


def test_init_refuses_an_empty_secret_instead_of_replacing_it(init_module, tmp_path):
    (tmp_path / "secrets_key").write_text("")
    with pytest.raises(SystemExit, match="a file in the secrets volume is empty") as stopped:
        init_module.fill(tmp_path)
    assert "secrets_key" not in str(stopped.value) and str(tmp_path) not in str(stopped.value)
    assert (tmp_path / "secrets_key").read_text() == ""


# ---- the entrypoint -----------------------------------------------------------------------------------------------


def _entrypoint(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ["PATH"]}
    return subprocess.run(
        ["sh", str(ENTRYPOINT), *args], env={**base, **env}, text=True, capture_output=True, timeout=60
    )


@pytest.mark.parametrize(
    "setting",
    ["EAP_DATABASE_URL", "EAP_DBOS_URL", "EAP_SECRETS_KEY", "EAP_SECRETS_OLD_KEYS", "EAP_AUDIT_HASH_SALT"],
)
def test_a_setting_is_read_from_its_file(tmp_path, setting):
    value = "from-a-file $(not run) 'quoted' %s"
    (tmp_path / "value").write_text(value + "\n")
    done = _entrypoint("run", "sh", "-c", f'printf %s "${setting}"', env={f"{setting}_FILE": str(tmp_path / "value")})
    assert (done.returncode, done.stdout) == (0, value), done.stderr


def test_the_plain_setting_wins_over_the_file(tmp_path):
    (tmp_path / "value").write_text("from-the-file")
    env = {"EAP_SECRETS_KEY": "from-the-environment", "EAP_SECRETS_KEY_FILE": str(tmp_path / "value")}
    done = _entrypoint("run", "sh", "-c", 'printf %s "$EAP_SECRETS_KEY"', env=env)
    assert done.stdout == "from-the-environment"


def test_an_empty_plain_setting_falls_back_to_the_file(tmp_path):
    (tmp_path / "value").write_text("from-the-file")
    env = {"EAP_SECRETS_KEY": "", "EAP_SECRETS_KEY_FILE": str(tmp_path / "value")}
    done = _entrypoint("run", "sh", "-c", 'printf %s "$EAP_SECRETS_KEY"', env=env)
    assert done.stdout == "from-the-file"


@pytest.mark.parametrize("content", [None, ""])
def test_a_missing_or_empty_file_stops_the_start_and_names_the_setting(tmp_path, content):
    path = tmp_path / "value"
    if content is not None:
        path.write_text(content)
    done = _entrypoint("run", "true", env={"EAP_DATABASE_URL_FILE": str(path)})
    assert done.returncode == 78
    assert "EAP_DATABASE_URL_FILE" in done.stderr


def test_other_file_settings_are_left_alone(tmp_path):
    """The sign-in plug-in reads its key file itself; nothing outside the list is expanded."""
    (tmp_path / "value").write_text("key")
    env = {"EAP_LOCAL_AUTH_SIGNING_KEY_FILE": str(tmp_path / "value")}
    done = _entrypoint("run", "sh", "-c", 'printf %s "${EAP_LOCAL_AUTH_SIGNING_KEY+set}"', env=env)
    assert (done.returncode, done.stdout) == (0, ""), done.stderr


def test_help_needs_no_secret_files(tmp_path):
    done = _entrypoint("help", env={"EAP_DATABASE_URL_FILE": str(tmp_path / "absent")})
    assert done.returncode == 0 and "setup-link" in done.stdout


def test_run_without_a_command_is_a_usage_error():
    assert _entrypoint("run", env={}).returncode == 64


# ---- the setup link -----------------------------------------------------------------------------------------------

CODE = "-".join(["ABCD", "EFGH", "IJKL", "MNOP", "QRST", "UVWX", "YZ23", "4567"])


def _setup_link(tmp_path: Path, *, status: int, line: str = "", env: dict[str, str] | None = None):
    """Run `setup-link` with a stand-in `python` that records its arguments, prints `line` and exits `status`."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "python").write_text(
        f'#!/bin/sh\necho "$@" >"{tmp_path}/args"\nprintf \'%s\\n\' "{line}"\nexit {status}\n'
    )
    (fake / "python").chmod(0o755)
    path = f"{fake}:{os.environ['PATH']}"
    done = _entrypoint("setup-link", env={"PATH": path, **(env or {})})
    assert (tmp_path / "args").read_text().strip() == "-m eap_iam_localadmin setup-code"
    return done


def test_setup_link_turns_the_code_into_a_link(tmp_path):
    line = f"eap localadmin setup code: {CODE}  (there is no admin account yet: enter this code)"
    done = _setup_link(tmp_path, status=0, line=line, env={"EAP_STUDIO_URL": "http://localhost:3100/"})
    assert (done.returncode, done.stdout) == (0, f"Open http://localhost:3100/setup#code={CODE}\n"), done.stderr


def test_setup_link_defaults_to_the_compose_port(tmp_path):
    done = _setup_link(tmp_path, status=0, line=f"setup code: {CODE}")
    assert done.stdout == f"Open http://localhost:3000/setup#code={CODE}\n"


def test_setup_link_says_when_the_admin_exists(tmp_path):
    done = _setup_link(tmp_path, status=3)
    assert done.returncode == 0
    assert "An admin account already exists" in done.stdout and "reset-password" in done.stdout


def test_setup_link_passes_a_failure_on(tmp_path):
    assert _setup_link(tmp_path, status=1).returncode == 1


def test_setup_link_fails_when_there_is_no_code(tmp_path):
    done = _setup_link(tmp_path, status=0, line="something else")
    assert done.returncode == 1 and "setup#code" not in done.stdout


# ---- documentation ------------------------------------------------------------------------------------------------


def test_the_guide_is_linked_and_names_the_commands():
    guide = (ROOT / "docs" / "quickstart-compose.md").read_text()
    for command in ("docker compose up -d", "docker compose exec api setup-link", "docker compose up -d --build",
                    "docker compose down -v", "reset-password"):
        assert command in guide, command
    assert "(quickstart-compose.md)" in (ROOT / "docs" / "expose.md").read_text()


# ---- the CI job leaves its Docker engine as it found it -----------------------------------------------------------

GUARD = ROOT / "scripts" / "ci" / "compose_runner.sh"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    # BaseLoader keeps YAML 1.1 from treating the key `on` as a boolean.
    return yaml.load((ROOT / ".github" / "workflows" / "compose.yml").read_text(), Loader=yaml.BaseLoader)


def test_a_pull_request_runs_the_job_only_when_it_touches_the_quick_start(workflow):
    assert set(workflow["on"]["pull_request"]["paths"]) == {
        "compose.yaml", ".env.example", ".dockerignore", "deploy/docker/**", "docs/quickstart-compose.md",
        "scripts/ci/compose_*", ".github/workflows/compose.yml",
    }
    assert workflow["on"]["push"]["branches"] == ["main"] and workflow["on"]["push"]["paths"]
    assert workflow["on"]["schedule"]


def test_the_job_always_cleans_up_and_caps_the_build_cache(workflow):
    last = workflow["jobs"]["quickstart"]["steps"][-1]
    assert last["if"] == "always()"
    assert last["run"].strip() == "bash scripts/ci/compose_runner.sh cleanup 15GB"


def test_the_operator_runner_checks_its_free_disk_before_building(workflow):
    runs = [step.get("run", "") for step in workflow["jobs"]["quickstart"]["steps"]]
    build = next(i for i, run in enumerate(runs) if "compose_smoke.py" in run)
    disk = [i for i, run in enumerate(runs) if "compose_runner.sh require-disk 25" in run]
    if "operator-only:" not in (ROOT / ".github" / "workflows" / "compose.yml").read_text():
        pytest.skip("the public tree carries no deploy VM")
    assert disk and disk[0] < build


def _guard(tmp_path: Path, *args: str, available_gb: int = 100, fail: str = "", old_buildx: bool = False):
    """Run the guard with a stand-in `docker` that logs each call; (result, calls)."""
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    cap_flag = "--keep-storage" if old_buildx else "--max-used-space"
    (fake / "docker").write_text(f"""#!/bin/sh
echo "$*" >>"{tmp_path}/calls"
case "$*" in
  *--help*) echo "      {cap_flag} bytes" ;;
  run*) printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\noverlay 1 1 {available_gb * 1024 * 1024} 1%% /\\n' ;;
esac
case "$*" in *"{fail or 'nothing fails'}"*) exit 1 ;; esac
""")
    (fake / "docker").chmod(0o755)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "PROJECT": "eap-smoke-1-1", "EAP_IMAGE_TAG": "smoke-1-1"}
    done = subprocess.run(["bash", str(GUARD), *args], env=env, text=True, capture_output=True, timeout=60)
    log = tmp_path / "calls"
    return done, log.read_text().splitlines() if log.exists() else []


def test_enough_free_disk_lets_the_job_start(tmp_path):
    done, calls = _guard(tmp_path, "require-disk", "25", available_gb=25)
    assert done.returncode == 0, done.stderr
    # --rm also removes the anonymous volume the probe image declares.
    assert calls == ["run --rm --network none --entrypoint df pgvector/pgvector:0.8.2-pg17-trixie -Pk /"]


def test_too_little_free_disk_refuses_with_a_clear_message(tmp_path):
    done, _ = _guard(tmp_path, "require-disk", "25", available_gb=24)
    assert done.returncode == 1
    assert "24 GB free" in done.stderr and "at least 25 GB" in done.stderr


def test_an_unreadable_free_disk_refuses_too(tmp_path):
    done, _ = _guard(tmp_path, "require-disk", "25", fail="run --rm")
    assert done.returncode != 0 and "free disk" in done.stderr


@pytest.mark.parametrize("old_buildx", [False, True])
def test_cleanup_removes_what_the_run_made_and_caps_the_cache(tmp_path, old_buildx):
    done, calls = _guard(tmp_path, "cleanup", "15GB", old_buildx=old_buildx)
    assert done.returncode == 0, done.stderr
    cap = "--keep-storage" if old_buildx else "--max-used-space"
    assert [c for c in calls if "--help" not in c and not c.startswith(("system df", "buildx inspect"))] == [
        "compose -p eap-smoke-1-1 down -v --remove-orphans",
        "image rm eap-python:smoke-1-1",
        "image rm eap-studio:smoke-1-1",
        "builder prune --force --filter until=72h",
        f"builder prune --force --all {cap} 15GB",
    ]


def test_cleanup_goes_on_after_a_failure_and_reports_a_failed_prune(tmp_path):
    done, calls = _guard(tmp_path, "cleanup", "15GB", fail="until=72h")
    assert done.returncode == 1
    assert any("--max-used-space 15GB" in c for c in calls)


def test_cleanup_of_a_run_that_never_built_succeeds(tmp_path):
    done, calls = _guard(tmp_path, "cleanup", "15GB", fail="image rm")
    assert done.returncode == 0, done.stderr
    assert sum("builder prune --force" in c for c in calls) == 2
