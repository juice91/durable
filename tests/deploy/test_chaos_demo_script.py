"""scripts/chaos_demo.sh: syntax, shellcheck, usage, --dry-run, and the whole flow against a fake API and fake commands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "chaos_demo.sh"
PARENT, CHILD, WF, KB = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222", "wf-1", "kb-1"

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")


def run_script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("EAP_", "CHAOS_"))}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env={**clean, **(env or {})},
                          cwd=REPO_ROOT, timeout=60)


def test_the_script_is_valid_bash_and_executable():
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True).returncode == 0


def test_the_script_passes_shellcheck():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    proc = subprocess.run([shellcheck, "-x", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_help_documents_the_environment_and_the_budget_and_the_schedule_warning():
    out = run_script("--help").stdout
    for word in ("EAP_API", "EAP_TOKEN", "CHAOS_KILL_CMD", "CHAOS_LOG_CMD", "CHAOS_DB_CMD", "CHAOS_PART_NUMBERS",
                 "CHAOS_KILL_AFTER_SEGMENTS", "KILL_AFTER_SECONDS", "3 provider", "Taipei"):
        assert word in out, word


def test_dry_run_prints_the_commands_and_the_block_headings_and_needs_no_token():
    proc = run_script("--dry-run")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "kubectl --context colima-pydanticos -n pydanticos delete pod worker-0" in out
    assert "logs worker-0" in out and "psql -U eap_admin" in out and "chaos-db" in out
    assert "/usr/local/bin" in out
    for heading in ("== 1. EVENTS", "== 2. WORKER LOG", "== 3. DBOS"):
        assert heading in out


def test_a_run_without_a_token_is_refused():
    proc = run_script()
    assert proc.returncode == 1 and "EAP_TOKEN" in proc.stderr


class FakeApi(BaseHTTPRequestHandler):
    kill_flag: Path
    duplicate_segment = False
    seen_auth: list[str] = []

    def log_message(self, *args):  # noqa: D401
        pass

    def _send(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        self.seen_auth.append(self.headers.get("Authorization", ""))
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/runs"):
            FakeApi.started = body
            return self._send({"run_id": PARENT}, 202)
        self._send({"id": WF}, 201)

    def do_GET(self):  # noqa: N802
        self.seen_auth.append(self.headers.get("Authorization", ""))
        killed = self.kill_flag.exists()
        p = self.path
        if p == "/api/v1/workflows":
            return self._send([{"id": WF, "name": "KB Source Synchronization", "archived": False, "latest_version": 1}])
        if p == f"/api/v1/workflows/{WF}":
            return self._send({"id": WF, "latest_version": 1})
        if p == f"/api/v1/runs/{PARENT}":
            return self._send({"id": PARENT, "status": "completed" if killed else "running"})
        if p == f"/api/v1/runs/{CHILD}":
            return self._send({"id": CHILD, "status": "completed" if killed else "running"})
        if p == f"/api/v1/runs/{PARENT}/events":
            events = [
                {"seq": 0, "type": "step_completed", "data": {"step_id": "source", "output": {"knowledge_base_id": KB}}},
                {"seq": 1, "type": "step_completed", "data": {"step_id": "sync_targeted", "output": {"run_id": CHILD}}},
            ]
            events += [{"seq": 2 + i, "type": "step_completed",
                        "data": {"step_id": "await_targeted", "scope": [{"loop": "wait_targeted", "index": i}]}}
                       for i in range(2 if killed else 1)]
            return self._send(events)
        if p == f"/api/v1/runs/{CHILD}/events":
            segs = [1, 1] if self.duplicate_segment else [1]
            return self._send([{"seq": i, "type": "ingest_progress", "data": {"phase": "sync_segment", "segment": s}}
                               for i, s in enumerate(segs)])
        if p.startswith(f"/api/v1/knowledge-bases/{KB}/sources/") and p.endswith(f"/syncs/{CHILD}"):
            return self._send({"status": "completed", "counters": {"discovered": 3}, "started_at": "t0", "completed_at": "t1"})
        self._send({"detail": "nope"}, 404)


FAKE_DB = """#!/usr/bin/env bash
sql="$(cat)"
case "$sql" in
  *"from kb_sources"*) printf 'id\\nsrc-1\\n' ;;
  *"limit 30"*) printf 'value\\nAAA1\\nBBB2\\n' ;;
  *workflow_status*) printf 'workflow_uuid|status|recovery_attempts|updated_at\\n%s|SUCCESS|1|now\\n%s:seg:1|SUCCESS|0|now\\n' "$RUN_ID" "$CHILD" ;;
  *"as records"*) printf 'records|distinct_records|entities|max_documents_per_entity|entities_with_duplicate_documents\\n6|6|2|1|%s\\n' "${FAKE_DUP_ENTITIES:-0}" ;;
esac
"""


@pytest.fixture
def fake(tmp_path):
    flag = tmp_path / "killed"
    FakeApi.kill_flag, FakeApi.duplicate_segment, FakeApi.seen_auth = flag, False, []
    server = HTTPServer(("127.0.0.1", 0), FakeApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    db = tmp_path / "fake_db.sh"
    db.write_text(FAKE_DB.replace('$CHILD', CHILD).replace('$RUN_ID', PARENT))
    db.chmod(0o755)
    env = {
        "EAP_API": f"http://127.0.0.1:{server.server_port}", "EAP_TOKEN": "secret-token-value",
        "CHAOS_KILL_CMD": f"touch {flag}", "CHAOS_LOG_CMD": "echo 'Recovering workflows for executor x'; echo unrelated",
        "CHAOS_DB_CMD": f"{db}", "KILL_AFTER_SECONDS": "5", "MAX_WAIT": "30",
    }
    yield env, flag
    server.shutdown()


@needs_jq
def test_the_whole_flow_against_a_fake_api(fake):
    env, flag = fake
    proc = run_script(env=env)
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert flag.exists()  # the kill command ran
    assert FakeApi.started == {"input": {"source_id": "src-1", "mode": "targeted", "part_numbers": ["AAA1", "BBB2"]}}
    for heading in ("== 1. EVENTS", "== 2. WORKER LOG", "== 3. DBOS"):
        assert heading in out
    assert "sync_targeted" in out and "segment 1  x1" in out and "<-- DUPLICATE" not in out
    assert "Recovering workflows for executor x" in out and "unrelated" not in out
    assert f"{PARENT}|SUCCESS|1|now" in out and ":seg:1|SUCCESS" in out
    assert "duplicate check: holds" in out and "PASS" in out
    assert {a for a in FakeApi.seen_auth} == {"Bearer secret-token-value"}
    assert "secret-token-value" not in out


@needs_jq
def test_a_segment_completed_twice_fails_the_run_of_the_script(fake):
    env, _ = fake
    FakeApi.duplicate_segment = True
    proc = run_script(env=env)
    assert proc.returncode == 1 and "segment 1  x2   <-- DUPLICATE" in proc.stdout and "FAIL" in proc.stdout


@needs_jq
def test_a_duplicate_found_by_the_database_check_fails_the_script(fake):
    env, _ = fake
    proc = run_script(env={**env, "FAKE_DUP_ENTITIES": "2"})
    assert proc.returncode == 1 and "duplicate check: FAILED" in proc.stdout
