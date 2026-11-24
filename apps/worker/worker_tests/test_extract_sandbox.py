"""P1.A3: the extraction sandbox runner (eap_worker.extract_sandbox) with the real child process
(`python -I -B -m eap_knowledge.sandbox`). No database."""

from __future__ import annotations

import asyncio
import base64
import os
import pickle
import sys
import time
from pathlib import Path

import pytest

from eap_knowledge import ExtractionFailed, ExtractLimits
from eap_worker import extract_sandbox
from eap_worker.extract_sandbox import CHILD_ENV, extract_in_sandbox, parse_result, run_sandbox, sandbox_command

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "knowledge"))
from knowledge_tests.pdf_fixtures import body_lines, build_pdf, flate_bomb_pdf, manual_pdf  # noqa: E402

FAST = ExtractLimits(extract_cpu_seconds=2)


@pytest.fixture(autouse=True)
def fresh_gate():
    """The concurrency gate is process-wide and sized once; each test sizes its own."""
    extract_sandbox.reset_gate_for_tests()
    yield
    extract_sandbox.reset_gate_for_tests()


def _run(coro):
    return asyncio.run(coro)


async def _fails(coro) -> ExtractionFailed:
    with pytest.raises(ExtractionFailed) as info:
        await coro
    return info.value


def test_command_is_isolated():
    assert sandbox_command()[1:] == ["-I", "-B", "-m", "eap_knowledge.sandbox"]
    assert sandbox_command(ExtractLimits(extract_cpu_seconds=7, extract_memory_mb=512))[5:] == [
        "--cpu", "7", "--memory", "512"]
    assert CHILD_ENV == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


def test_child_environment_has_no_secrets(monkeypatch):
    monkeypatch.setenv("EAP_LITELLM_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("EAP_DATABASE_URL", "postgresql://secret@db/x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-also-secret")
    env = _run(run_sandbox({"eap_test": "env"}, FAST))["env"]
    assert not [k for k in env if k.startswith("EAP_") or k.endswith("_API_KEY") or k.startswith("PYTHON")]
    assert "sk-should-not-leak" not in repr(env)
    assert env["PATH"] == "/usr/bin:/bin" and env["LANG"] == "C.UTF-8"
    # macOS CoreFoundation may add __CF_USER_TEXT_ENCODING itself; nothing else is inherited.
    assert set(env) - {"PATH", "LANG", "__CF_USER_TEXT_ENCODING", "LC_CTYPE"} == set()


@pytest.mark.parametrize("how", ["open", "append", "os_open"])
def test_creating_a_file_is_refused_even_empty(tmp_path, how):
    target = tmp_path / "probe.bin"
    result = _run(run_sandbox({"eap_test": "write", "arg": str(target), "how": how}, FAST))
    assert result["wrote"] is False and result["errno"] == 13  # EACCES: the audit hook
    assert not target.exists()  # not even an empty file


@pytest.mark.parametrize("how", ["open", "truncate", "append"])
def test_an_existing_file_is_never_truncated_or_changed(tmp_path, how):
    target = tmp_path / "existing.txt"
    target.write_bytes(b"keep me")
    result = _run(run_sandbox({"eap_test": "write", "arg": str(target), "how": how}, FAST))
    assert result["wrote"] is False and result["errno"] == 13
    assert target.read_bytes() == b"keep me"


def test_a_network_connection_is_refused_by_the_audit_hook():
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(0.5)
    try:
        port = server.getsockname()[1]
        result = _run(run_sandbox({"eap_test": "connect", "arg": ["127.0.0.1", port]}, FAST))
        assert result == {"ok": True, "connected": False, "error": "PermissionError", "errno": 13}
        with pytest.raises(TimeoutError):
            server.accept()  # nothing ever reached the listener
        named = _run(run_sandbox({"eap_test": "connect", "arg": ["localhost", port]}, FAST))
        assert named["connected"] is False and named["error"] == "PermissionError"  # getaddrinfo refused
    finally:
        server.close()


from eap_knowledge.sandbox import _PROBES, _REFUSED_IMPORTS  # noqa: E402


@pytest.mark.parametrize("probe", sorted(_PROBES))
def test_every_probe_is_refused_by_the_audit_hook(tmp_path, probe):
    victim = tmp_path / "victim"
    victim.mkdir() if probe in ("fs:rmdir", "fs:rmtree") else victim.write_text("keep")
    result = _run(run_sandbox({"eap_test": "probe", "arg": probe, "path": str(victim)}, FAST))
    expected = "AttributeError" if probe == "fs:mkfifo" else "PermissionError"  # mkfifo is removed
    assert result == {"ok": True, "done": False, "error": expected}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["victim"]  # nothing created, moved or removed
    if victim.is_file():
        assert victim.read_text() == "keep"


@pytest.mark.parametrize("probe", ["socket", "resolve:gethostbyname", "resolve:getnameinfo", "fs:mkdir", "fs:rename", "subprocess"])
def test_the_probes_work_without_the_hook(tmp_path, probe):
    # Positive control, in this (unhooked) test process: each probe really does its operation, so
    # the refusals above come from the hook, not from a broken probe.
    victim = tmp_path / "victim"
    victim.write_text("x")
    _PROBES[probe](str(victim))
    if probe == "fs:mkdir":
        assert (tmp_path / "victim.dir").is_dir()
    if probe == "fs:rename":
        assert (tmp_path / "victim.moved").exists()


def test_no_refused_module_is_loaded_before_the_hook():
    import subprocess as sp

    code = (
        "import sys, eap_knowledge.sandbox as s; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in s._REFUSED_IMPORTS))"
    )
    out = sp.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
    assert {"ctypes", "_ctypes", "sqlite3", "_sqlite3"} <= _REFUSED_IMPORTS


def test_fork_exec_is_replaced_and_cannot_be_reloaded():
    from eap_knowledge.sandbox import _REFUSED_FRESH_LOADS

    assert "_posixsubprocess" in _REFUSED_FRESH_LOADS
    for probe in ("fork_exec", "fork_exec_reload", "subprocess"):
        result = _run(run_sandbox({"eap_test": "probe", "arg": probe}, FAST))
        assert result == {"ok": True, "done": False, "error": "PermissionError"}


def test_a_spawn_failure_is_transient_and_pickle_safe(monkeypatch):
    import errno

    from eap_worker.kb_ingest import _transient

    async def no_fork(*args, **kwargs):
        raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(extract_sandbox.asyncio, "create_subprocess_exec", no_fork)
    with pytest.raises(extract_sandbox.SandboxSpawnFailed) as info:
        _run(run_sandbox({"eap_test": "env"}, FAST))
    assert info.value.errno == errno.EAGAIN and _transient(info.value)
    back = pickle.loads(pickle.dumps(info.value))
    assert back.errno == errno.EAGAIN and _transient(back)


def test_the_gate_is_one_per_process_and_keeps_its_size():
    async def use(limits):
        return await run_sandbox({"eap_test": "env"}, limits)

    _run(use(ExtractLimits(extract_concurrency=2)))
    gate = extract_sandbox._gate
    _run(use(ExtractLimits(extract_concurrency=5)))  # another loop, another limit: the same gate
    assert extract_sandbox._gate is gate and gate.limit == 2 and gate.running == 0


def test_oversized_output_is_extract_output_too_large():
    exc = _run(_fails(run_sandbox({"eap_test": "output", "arg": 5000}, FAST, max_output_bytes=1000)))
    assert exc.code == "extract_output_too_large"


def test_cpu_limit_is_extract_timeout():
    start = time.monotonic()
    exc = _run(_fails(run_sandbox({"eap_test": "spin", "arg": 30}, ExtractLimits(extract_cpu_seconds=1))))
    assert exc.code == "extract_timeout" and time.monotonic() - start < 10


def test_wall_timeout_kills_the_child():
    start = time.monotonic()
    exc = _run(_fails(run_sandbox({"eap_test": "sleep", "arg": 30}, ExtractLimits(extract_cpu_seconds=1))))
    assert exc.code == "extract_timeout" and time.monotonic() - start < 10  # 2 x the CPU limit


def test_a_crash_is_extract_failed():
    exc = _run(_fails(run_sandbox({"eap_test": "crash"}, FAST)))
    assert exc.code == "extract_failed" and exc.detail == "exit status 70"  # a hard exit, never SIGABRT


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_AS works on Linux only")
def test_memory_limit_is_extract_memory():
    limits = ExtractLimits(extract_memory_mb=256)
    assert _run(_fails(run_sandbox({"eap_test": "alloc", "arg": 1024}, limits))).code == "extract_memory"


def test_semaphore_bounds_concurrency():
    async def go() -> float:
        start = time.monotonic()
        limits = ExtractLimits(extract_concurrency=2)
        await asyncio.gather(*[run_sandbox({"eap_test": "sleep", "arg": 1}, limits) for _ in range(4)])
        return time.monotonic() - start

    elapsed = _run(go())
    assert elapsed >= 2.0  # 4 one-second children, two at a time


def test_semaphore_counts_running_children(monkeypatch):
    running = 0
    peak = 0
    real = asyncio.create_subprocess_exec

    async def counting(*args, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        proc = await real(*args, **kwargs)
        real_wait = proc.wait

        async def wait():
            nonlocal running
            try:
                return await real_wait()
            finally:
                if proc.returncode is not None and not getattr(proc, "_counted", False):
                    proc._counted = True
                    running -= 1

        proc.wait = wait
        return proc

    monkeypatch.setattr(extract_sandbox.asyncio, "create_subprocess_exec", counting)

    async def go():
        limits = ExtractLimits(extract_concurrency=3)
        await asyncio.gather(*[run_sandbox({"eap_test": "sleep", "arg": 0.3}, limits) for _ in range(7)])

    _run(go())
    assert peak == 3


def test_pdf_end_to_end():
    content = base64.b64encode(manual_pdf()).decode()
    result = _run(extract_in_sandbox("pdf", content, source_url="https://h/trm.pdf", limits=FAST))
    assert result.pages == 6 and result.image_only_pages == (6,)
    assert result.document.format == "pdf" and result.document.sections[0].anchor == "page=1"
    assert result.document.sections[0].page == 1


def test_code_extraction_runs_in_the_sandbox_under_the_audit_hook():
    source = "import os\n\n\ndef a():\n    return os.name\n\n\nclass B:\n    pass\n"
    code = {"language": "python", "path": "pkg/m.py", "kind": "test"}
    result = _run(extract_in_sandbox("code", source, source_url="https://h/m.py", max_tokens=512,
                                     overlap_tokens=32, code=code, limits=FAST))
    assert result.document.format == "code"
    got = [(s.code.symbol, s.code.start_line, s.code.end_line, s.code.kind, s.anchor) for s in result.document.sections]
    assert got == [("<module>", 1, 1, "test", "L1-L1"), ("a", 4, 5, "test", "L4-L5"), ("B", 8, 9, "test", "L8-L9")]
    assert result.document.sections[1].text.startswith("# pkg/m.py:4-5 a\n")


def test_code_extraction_needs_a_language_and_a_path():
    exc = _run(_fails(extract_in_sandbox("code", "x = 1", code={"language": "cobol", "path": "a"}, limits=FAST)))
    assert exc.code == "extract_failed"
    exc = _run(_fails(extract_in_sandbox("code", "x = 1", code=None, limits=FAST)))
    assert exc.code == "extract_failed"


def test_code_extraction_reports_generated_files_and_refuses_binary():
    code = {"language": "javascript", "path": "a.js"}
    result = _run(extract_in_sandbox("code", "// Code generated. DO NOT EDIT.\nvar a = 1;\n", code=code, limits=FAST))
    assert result.document.sections == () and result.document.warnings == ("generated_skipped",)
    exc = _run(_fails(extract_in_sandbox("code", "a\x00b", code=code, limits=FAST)))
    assert exc.code == "unsupported_content"


def test_options_and_limits_reach_the_child():
    content = base64.b64encode(manual_pdf()).decode()
    result = _run(extract_in_sandbox("pdf", content, options={"pdf_headings": "pages"}, limits=FAST))
    assert all(s.heading_path == () for s in result.document.sections)
    exc = _run(_fails(extract_in_sandbox("pdf", content, limits=ExtractLimits(max_pdf_pages=3))))
    assert exc.code == "extract_limit"


def test_child_failure_codes_are_pickle_safe():
    content = base64.b64encode(build_pdf([body_lines(0)], encrypt="pw")).decode()
    exc = _run(_fails(extract_in_sandbox("pdf", content, limits=FAST)))
    assert exc.code == "document_encrypted" and exc.user_message == "The PDF is encrypted with a password."
    back = pickle.loads(pickle.dumps(exc))
    assert (back.code, back.detail) == (exc.code, exc.detail)


def test_inflate_bomb_fails_and_the_worker_lives():
    content = base64.b64encode(flate_bomb_pdf()).decode()
    exc = _run(_fails(extract_in_sandbox("pdf", content, limits=ExtractLimits(extract_cpu_seconds=20))))
    assert exc.code in ("extract_limit", "extract_memory", "extract_timeout")
    assert os.getpid()  # still here: the bomb ran in the child
    ok = _run(extract_in_sandbox("pdf", base64.b64encode(manual_pdf()).decode(), limits=FAST))
    assert ok.pages == 6


def test_polyglot_fails():
    content = base64.b64encode(b"%PDF-1.7\n<html><script>x</script></html>").decode()
    assert _run(_fails(extract_in_sandbox("pdf", content, limits=FAST))).code == "extract_failed"


@pytest.mark.parametrize(
    "result",
    [
        {"ok": True},
        {"ok": True, "document": {"sections": "x"}},
        {"ok": True, "document": {"title": 1, "format": "pdf", "sections": [], "extracted_chars": 0}},
        {"ok": True, "document": {"title": "", "format": "pdf", "sections": [{"heading_path": "x"}], "extracted_chars": 0}},
        {"ok": True, "document": {"title": "", "format": "pdf", "sections": [], "extracted_chars": -1}},
    ],
)
def test_malformed_results_are_extract_failed(result):
    with pytest.raises(ExtractionFailed) as info:
        parse_result(result)
    assert info.value.code == "extract_failed"


def test_unknown_child_code_reads_as_extract_failed():
    with pytest.raises(ExtractionFailed) as info:
        parse_result({"ok": False, "code": "weird", "detail": "x"})
    assert info.value.code == "extract_failed"


def test_the_gate_is_first_come_first_served_and_cancel_safe():
    gate = extract_sandbox._Gate(1)
    order: list[int] = []

    async def go() -> None:
        async def worker(i: int, hold: float) -> None:
            async with gate:
                order.append(i)
                await asyncio.sleep(hold)

        first = asyncio.create_task(worker(0, 0.05))
        await asyncio.sleep(0)
        waiters = [asyncio.create_task(worker(i, 0.01)) for i in range(1, 6)]
        await asyncio.sleep(0.01)
        waiters[1].cancel()  # cancelled while waiting: never runs, never holds a slot
        await asyncio.gather(first, *waiters, return_exceptions=True)

    _run(go())
    assert order == [0, 1, 3, 4, 5] and gate.running == 0


def test_a_waiter_cancelled_after_the_hand_over_passes_the_slot_on():
    gate = extract_sandbox._Gate(1)
    ran: list[str] = []

    async def go() -> None:
        await gate.__aenter__()
        waiting = asyncio.create_task(gate.__aenter__())
        after = asyncio.create_task(gate.__aenter__())
        await asyncio.sleep(0)
        await gate.__aexit__()  # hands the slot to `waiting` ...
        waiting.cancel()  # ... which is cancelled before it resumes
        await asyncio.gather(waiting, return_exceptions=True)
        await asyncio.wait_for(after, 1)  # the slot was passed on
        ran.append("after")
        await gate.__aexit__()

    _run(go())
    assert ran == ["after"] and gate.running == 0
