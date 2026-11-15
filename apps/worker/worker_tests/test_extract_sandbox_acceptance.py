"""P1.A3 acceptance: the extraction sandbox (eap_worker.extract_sandbox) with the real child process:
isolation, limits, crash mapping, zombies, concurrency, hostile PDFs. No database."""

from __future__ import annotations

import asyncio
import base64
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from eap_knowledge import ExtractionFailed, ExtractLimits
from eap_worker import extract_sandbox
from eap_worker.extract_sandbox import CHILD_ENV, extract_in_sandbox, run_sandbox

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "knowledge"))
from knowledge_tests.pdf_fixtures import body_lines, build_pdf, flate_bomb_pdf, manual_pdf  # noqa: E402
from knowledge_tests.test_p1a3_pdf_acceptance import CATALOG, FONT, ONE_PAGE, PAGE, raw_pdf  # noqa: E402

FAST = ExtractLimits(extract_cpu_seconds=2)
MARKER = "eap_knowledge.sandbox"


@pytest.fixture(autouse=True)
def fresh_gate():
    """The concurrency gate is process-wide and sized by its first use; each test sizes its own."""
    extract_sandbox.reset_gate_for_tests()
    yield
    extract_sandbox.reset_gate_for_tests()


def run(coro):
    return asyncio.run(coro)


async def fails(coro) -> ExtractionFailed:
    with pytest.raises(ExtractionFailed) as info:
        await coro
    return info.value


def my_children(*, only_sandbox: bool = True) -> list[tuple[int, str, str]]:
    """(pid, stat, command) of this process's direct children."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,stat=,command="], capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and int(parts[1]) == os.getpid() and (MARKER in parts[3] or not only_sandbox):
            rows.append((int(parts[0]), parts[2], parts[3]))
    return rows


def assert_no_children(wait: float = 3.0) -> None:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and my_children():
        time.sleep(0.05)
    assert my_children() == [], "sandbox child (or zombie) left behind"


# ------------------------------------------------------------------------------- isolation


def test_child_sees_no_parent_secret_of_any_name(monkeypatch):
    secrets = {
        "FAKE_WORKER_SECRET": "s3cr3t-1", "AWS_SECRET_ACCESS_KEY": "s3cr3t-2", "PGPASSWORD": "s3cr3t-3",
        "ANTHROPIC_API_KEY": "s3cr3t-4", "EAP_KB_EXTRACT_CONCURRENCY": "2", "HOME": "/home/secret-user",
        "PYTHONPATH": "/evil", "LD_PRELOAD": "/evil.so", "DYLD_INSERT_LIBRARIES": "/evil.dylib",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    env = run(run_sandbox({"eap_test": "env"}, FAST))["env"]
    assert set(env) - {"PATH", "LANG", "__CF_USER_TEXT_ENCODING", "LC_CTYPE"} == set()
    assert "s3cr3t" not in repr(env) and "/evil" not in repr(env) and "secret-user" not in repr(env)
    assert {k: env[k] for k in CHILD_ENV} == CHILD_ENV


def test_document_text_cannot_switch_on_a_test_hook():
    # A document that looks like a hook payload is just content for the JSON extractor.
    content = '{"eap_test": "env", "arg": "x"}'
    result = run(extract_in_sandbox("json", content, limits=FAST))
    assert result.document.format == "json" and "PATH" not in repr(result.document)


def test_the_child_makes_no_network_connection_while_parsing_hostile_links():
    hits: list[str] = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.2)
    port = server.getsockname()[1]
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except (TimeoutError, OSError):
                continue
            hits.append("connection")
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/x"
    try:
        rst = f"T\n=\n\n.. include:: {url}\n\n.. raw:: html\n   <img src='{url}'>\n\n.. image:: {url}\n\ntext here\n"
        run(extract_in_sandbox("rst", rst, limits=FAST))
        yaml = f"a: !!python/object/apply:urllib.request.urlopen ['{url}']\n"
        with pytest.raises(ExtractionFailed):
            run(extract_in_sandbox("yaml", yaml, limits=FAST))
        run(extract_in_sandbox("markdown-doxygen", f"\\page p P\n![x]({url}) [y]({url})\n", limits=FAST))
        action = f"<< /S /URI /URI ({url}) >>".encode()
        page = PAGE[:-3] + b" /Annots [<< /Type /Annot /Subtype /Link /A " + action + b" >>] >>"
        pdf = raw_pdf([CATALOG, ONE_PAGE, page, b"<< /Length 20 >>\nstream\nBT ET\nendstream", FONT])
        with pytest.raises(ExtractionFailed):
            run(extract_in_sandbox("pdf", base64.b64encode(pdf).decode(), limits=FAST))
        # The audit hook refuses a direct attempt too (the parsers above never even tried).
        direct = run(run_sandbox({"eap_test": "connect", "arg": ["127.0.0.1", port]}, FAST))
        assert direct == {"ok": True, "connected": False, "error": "PermissionError", "errno": 13}
        time.sleep(0.3)
    finally:
        stop.set()
        thread.join(2)
        server.close()
    assert hits == []


@pytest.mark.parametrize("where", ["tmp", "cwd_relative", "home_relative"])
def test_writes_are_refused_everywhere(tmp_path, where):
    target = {
        "tmp": str(tmp_path / "w.bin"), "cwd_relative": "eap-sandbox-probe.bin",
        "home_relative": str(Path.home() / ".eap-sandbox-probe"),
    }[where]
    for how in ("open", "os_open", "append"):
        result = run(run_sandbox({"eap_test": "write", "arg": target, "how": how}, FAST))
        assert result["wrote"] is False and result["errno"] == 13  # EACCES: the audit hook
    for candidate in (Path(target), Path("/") / target, Path.cwd() / target):
        assert not candidate.exists()  # not even an empty file


def test_an_existing_file_is_never_truncated(tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("original")
    for how in ("open", "truncate"):
        result = run(run_sandbox({"eap_test": "write", "arg": str(target), "how": how}, FAST))
        assert result["wrote"] is False and result["errno"] == 13
    assert target.read_text() == "original"


# ------------------------------------------------------------------------------- limits


def test_output_cap_boundary_and_garbage_before_json():
    over = run(fails(run_sandbox({"eap_test": "output", "arg": 2000}, FAST, max_output_bytes=1000)))
    assert over.code == "extract_output_too_large"
    # Under the cap but not JSON (x's then the hook's result): malformed, not "too large".
    bad = run(fails(run_sandbox({"eap_test": "output", "arg": 100}, FAST, max_output_bytes=1000)))
    assert bad.code == "extract_failed"


def test_the_default_output_cap_is_64_mib():
    start = time.monotonic()
    exc = run(fails(run_sandbox({"eap_test": "output", "arg": 70 * 1024 * 1024}, ExtractLimits(extract_cpu_seconds=20))))
    assert exc.code == "extract_output_too_large" and time.monotonic() - start < 30
    assert_no_children()


def test_a_real_extraction_over_a_tiny_cap_is_too_large():
    # The cap applies to real results too (run_sandbox is what extract_in_sandbox uses).
    payload = {"extractor": "pdf", "content": base64.b64encode(manual_pdf()).decode()}
    assert run(fails(run_sandbox(payload, FAST, max_output_bytes=200))).code == "extract_output_too_large"


def test_timeouts_leave_no_child_and_the_limits_are_per_call():
    start = time.monotonic()
    assert run(fails(run_sandbox({"eap_test": "spin", "arg": 60}, ExtractLimits(extract_cpu_seconds=1)))).code == "extract_timeout"
    assert run(fails(run_sandbox({"eap_test": "sleep", "arg": 60}, ExtractLimits(extract_cpu_seconds=1)))).code == "extract_timeout"
    assert time.monotonic() - start < 12
    assert_no_children()
    assert run(run_sandbox({"eap_test": "sleep", "arg": 0.1}, FAST))["ok"] is True  # no state leaked


def test_failure_details_never_carry_document_text():
    secret = "SECRET-DOC-TEXT"
    for extractor, content in [("rst", f"{secret}\n.. bogus-directive:: {secret}\n"), ("yaml", f"{secret}: [\n"),
                               ("json", f'{{"{secret}": '), ("pdf", base64.b64encode(f"%PDF-1.7 {secret}".encode()).decode())]:
        try:
            run(extract_in_sandbox(extractor, content, limits=FAST))
        except ExtractionFailed as exc:
            assert secret not in str(exc) and secret not in exc.detail and secret not in exc.user_message


# ------------------------------------------------------------------------------- crashes


def test_abort_is_extract_failed_and_leaves_no_zombie():
    assert run(fails(run_sandbox({"eap_test": "crash"}, FAST))).code == "extract_failed"
    assert_no_children()


def _kill_child_after(sig: int, delay: float = 0.6) -> threading.Thread:
    def go() -> None:
        time.sleep(delay)
        for pid, _, _ in my_children():
            os.kill(pid, sig)

    thread = threading.Thread(target=go)
    thread.start()
    return thread


@pytest.mark.parametrize(
    ("sig", "code"),
    # SIGUSR1, not SIGSEGV or SIGABRT: those make the macOS crash reporter write a report.
    [(signal.SIGUSR1, "extract_failed"), (signal.SIGTERM, "extract_failed"), (signal.SIGKILL, "extract_memory")],
)
def test_signals_from_outside_are_mapped(sig, code):
    killer = _kill_child_after(sig)
    try:
        exc = run(fails(run_sandbox({"eap_test": "sleep", "arg": 30}, FAST)))
    finally:
        killer.join()
    assert exc.code == code
    assert_no_children()


def test_a_crash_does_not_stop_the_next_extraction():
    run(fails(run_sandbox({"eap_test": "crash"}, FAST)))
    result = run(extract_in_sandbox("pdf", base64.b64encode(manual_pdf()).decode(), limits=FAST))
    assert result.pages == 6


# ------------------------------------------------------------------------------- concurrency


def _peak_children(n_tasks: int, hold: float, limit_env: str | None, monkeypatch) -> int:
    if limit_env is not None:
        monkeypatch.setenv("EAP_KB_EXTRACT_CONCURRENCY", limit_env)
    # The gate is process-wide and sized by its first use (a worker reads the setting once); a new
    # setting therefore needs a new gate, as a restarted worker would get.
    extract_sandbox.reset_gate_for_tests()
    peak = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, len(my_children()))
            time.sleep(0.03)

    sampler = threading.Thread(target=sample)
    sampler.start()
    try:
        async def go() -> None:
            await asyncio.gather(*[run_sandbox({"eap_test": "sleep", "arg": hold}) for _ in range(n_tasks)])

        run(go())
    finally:
        stop.set()
        sampler.join()
    return peak


def test_the_env_concurrency_setting_bounds_live_children(monkeypatch):
    assert _peak_children(4, 0.6, "1", monkeypatch) == 1
    assert _peak_children(6, 0.8, "3", monkeypatch) == 3
    assert_no_children()


def test_default_concurrency_is_two(monkeypatch):
    monkeypatch.delenv("EAP_KB_EXTRACT_CONCURRENCY", raising=False)
    assert _peak_children(5, 0.6, None, monkeypatch) == 2


def test_a_failing_child_releases_its_slot(monkeypatch):
    monkeypatch.setenv("EAP_KB_EXTRACT_CONCURRENCY", "1")

    async def go():
        results = await asyncio.gather(
            *[run_sandbox({"eap_test": "crash"}) for _ in range(3)], run_sandbox({"eap_test": "env"}),
            return_exceptions=True,
        )
        return results

    results = run(go())
    assert [type(r).__name__ for r in results[:3]] == ["ExtractionFailed"] * 3 and results[3]["ok"] is True


# ------------------------------------------------------------------------------- hostile PDFs


def _pdf_code(data: bytes, limits: ExtractLimits = FAST) -> str:
    exc = run(fails(extract_in_sandbox("pdf", base64.b64encode(data).decode(), limits=limits)))
    return exc.code


def test_hostile_pdfs_fail_with_codes_inside_the_sandbox_and_the_worker_lives():
    start = time.monotonic()
    page = PAGE[:-3] + b" /Resources << /Font << /F1 5 0 R >> >> >>"
    nested = raw_pdf([CATALOG, ONE_PAGE, page, b"[" * 300_000 + b"]" * 300_000, FONT])
    assert _pdf_code(nested) == "extract_limit"
    content = b"<< /Length 47 >>\nstream\nBT /F1 12 Tf 72 700 Td (Hello world text) Tj ET\nendstream"
    good = raw_pdf([CATALOG, ONE_PAGE, page, content, FONT])
    assert run(extract_in_sandbox("pdf", base64.b64encode(good).decode(), limits=FAST)).pages == 1
    huge_xref = good.replace(b"xref\n0 6\n", b"xref\n0 900000000\n")
    assert huge_xref != good
    assert _pdf_code(huge_xref) == "extract_limit"
    assert _pdf_code(b"%PDF-1.7\n<html><script>alert(1)</script></html>") == "extract_failed"
    assert _pdf_code(b"%PDF-1.7\nPK\x03\x04" + os.urandom(2000)) in ("extract_failed", "no_extractable_text")
    assert time.monotonic() - start < 20
    assert run(extract_in_sandbox("pdf", base64.b64encode(manual_pdf()).decode(), limits=FAST)).pages == 6
    assert_no_children()


def test_a_flate_bomb_is_bounded_in_time_and_code_and_leaves_no_child():
    start = time.monotonic()
    code = _pdf_code(flate_bomb_pdf(), ExtractLimits(extract_cpu_seconds=20))
    assert code in ("extract_limit", "extract_memory", "extract_timeout") and time.monotonic() - start < 45
    assert_no_children()


def test_a_page_over_the_limit_is_rejected_before_text_work():
    pdf = build_pdf([body_lines(i) for i in range(30)])
    assert _pdf_code(pdf, ExtractLimits(extract_cpu_seconds=2, max_pdf_pages=29)) == "extract_limit"
    ok = run(extract_in_sandbox("pdf", base64.b64encode(pdf).decode(), limits=ExtractLimits(max_pdf_pages=30)))
    assert ok.pages == 30


def test_the_encrypted_and_empty_password_paths_through_the_child():
    enc = build_pdf([body_lines(0)], encrypt="real-password")
    assert _pdf_code(enc) == "document_encrypted"
    ok = run(extract_in_sandbox("pdf", base64.b64encode(build_pdf([body_lines(0)], encrypt="")).decode(), limits=FAST))
    assert ok.pages == 1
