"""The extraction sandbox runner (RAG P1, task P1.A3): parse an uploaded document in a disposable child.

``extract_in_sandbox(...)`` runs ``python -I -B -m eap_knowledge.sandbox`` through
``asyncio.create_subprocess_exec``, writes the payload to its stdin and reads its JSON result:

- **Environment:** only ``PATH`` (a fixed system path) and ``LANG=C.UTF-8``: no ``EAP_*`` setting, no
  provider key and no database URL reaches the child. ``-I`` also ignores ``PYTHON*`` variables and
  the user site; ``-B`` writes no bytecode. The working directory is ``/``.
- **Limits:** passed as arguments (``--cpu``, ``--memory``) and set by the child itself as the first
  thing in ``eap_knowledge.sandbox.main``, before it reads the payload or imports a parser module
  (only the ``eap_knowledge`` package itself, with ``markdown_it`` and ``pydantic``, is loaded; no
  input has been seen) (no
  ``preexec_fn``, which is unsafe in the threaded worker): ``RLIMIT_CPU`` EAP_KB_EXTRACT_CPU_SECONDS
  (default 60); on Linux ``RLIMIT_AS`` EAP_KB_EXTRACT_MEMORY_MB (default 1024; macOS has no working
  ``RLIMIT_AS``, so dev relies on the input caps and the timeout); ``RLIMIT_FSIZE`` 0 (no file
  grows); ``RLIMIT_NOFILE`` 32; ``RLIMIT_CORE`` 0. The child runs in its own session.
- **Audit hook:** the child refuses sockets and resolver calls, process starts (``fork_exec``
  included), signals, native loaders (``ctypes``, ``sqlite3``), opening a file for writing,
  creating or truncating, and file-system changes (remove, rename, mkdir, symlink, ...), with
  PermissionError. This is in-interpreter defence in depth, not OS isolation (see
  eap_knowledge.sandbox).
- **Wall timeout:** 2 x the CPU limit for the whole call (reading the output and waiting for the
  exit share one deadline), then the child is killed.
- **Output:** at most EXTRACT_MAX_OUTPUT_BYTES (64 MiB) of JSON; more kills the child.
- **Concurrency:** at most EAP_KB_EXTRACT_CONCURRENCY (default 2) children per worker process: one
  process-wide, first-come-first-served gate, sized once by the first call (independent of event
  loops).
- **Spawn failures** (EAGAIN, ENOMEM, EMFILE from fork/exec) raise SandboxSpawnFailed, which the
  ingest step's DBOS retry treats as transient.

Outcomes: a Document (with the PDF page count and image-only pages), or ``ExtractionFailed(code)``,
which is pickle-safe: ``extract_timeout`` (wall timeout or SIGXCPU), ``extract_memory`` (MemoryError
in the child, its MEMORY_EXIT status, or a SIGKILL the worker did not send), ``extract_output_too_large``,
the child's own code (``document_encrypted``, ``extract_limit``, ...), or ``extract_failed`` for
anything else (a crash, another signal, a malformed result). None of them carries document text;
``detail`` (type names and limits only) is never stored or shown.

The worker process never imports a parser (an import-linter contract), so a parser bug or a bomb
kills the child, not the worker.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from eap_knowledge import CodeRef, Document, ExtractionFailed, ExtractLimits, Section
from eap_worker.config import EXTRACT_MAX_OUTPUT_BYTES, extract_limits

SANDBOX_MODULE = "eap_knowledge.sandbox"
CHILD_PATH = "/usr/bin:/bin"
CHILD_ENV: dict[str, str] = {"PATH": CHILD_PATH, "LANG": "C.UTF-8"}
MEMORY_EXIT = 75  # eap_knowledge.sandbox.MEMORY_EXIT (not imported: the worker never imports the sandbox)
_READ_CHUNK = 1024 * 1024

__all__ = [
    "CHILD_ENV",
    "SandboxResult",
    "SandboxSpawnFailed",
    "extract_in_sandbox",
    "run_sandbox",
    "sandbox_command",
]


@dataclass(frozen=True)
class SandboxResult:
    document: Document
    pages: int | None = None
    image_only_pages: tuple[int, ...] = ()


class SandboxSpawnFailed(Exception):
    """The child could not be started (fork/exec failed: EAGAIN, ENOMEM, EMFILE, ...). Transient:
    the ingest step's DBOS retry tries again. Pickle-safe; carries the errno only."""

    def __init__(self, errno: int | None) -> None:
        super().__init__(errno)
        self.errno = errno

    def __str__(self) -> str:
        return f"the extraction sandbox could not be started (errno {self.errno})"


def sandbox_command(limits: ExtractLimits | None = None) -> list[str]:
    command = [sys.executable, "-I", "-B", "-m", SANDBOX_MODULE]
    if limits is not None:
        command += ["--cpu", str(limits.extract_cpu_seconds), "--memory", str(limits.extract_memory_mb)]
    return command


class _Gate:
    """A process-wide, first-come-first-served counting gate for async callers on any event loop (a
    worker has one loop; tests run several). A waiter parks on a future of its own loop and is
    woken through ``call_soon_threadsafe`` when a slot is handed to it, so no slot sits idle and
    no waiter is overtaken. A waiter cancelled after its slot was handed over passes it on."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.running = 0
        self._lock = threading.Lock()
        self._waiters: deque[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = deque()

    async def __aenter__(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self.running < self.limit and not self._waiters:
                self.running += 1
                return
            future: asyncio.Future[None] = loop.create_future()
            self._waiters.append((loop, future))
        try:
            await future  # resolved: the releasing caller handed its slot to us (running unchanged)
        except asyncio.CancelledError:
            with self._lock:
                try:
                    self._waiters.remove((loop, future))
                    handed = False
                except ValueError:
                    handed = True
            if handed:
                self._release()
            raise

    def _release(self) -> None:
        with self._lock:
            while self._waiters:
                loop, future = self._waiters.popleft()
                if loop.is_closed():
                    continue  # its task is gone
                try:
                    loop.call_soon_threadsafe(_hand_over, future)
                except RuntimeError:  # the loop closed just now
                    continue
                return
            self.running -= 1

    async def __aexit__(self, *exc: object) -> None:
        self._release()


def _hand_over(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)
    # A future cancelled meanwhile: its waiter's CancelledError handler finds itself gone from the
    # queue and passes the slot on.


_gate: _Gate | None = None
_gate_lock = threading.Lock()


def _process_gate(limit: int) -> _Gate:
    """The one gate of this process, sized by the first call; later limits do not resize it."""
    global _gate
    with _gate_lock:
        if _gate is None:
            _gate = _Gate(limit)
        return _gate


def reset_gate_for_tests() -> None:
    """Tests only: forget the gate, so the next call sizes a new one."""
    global _gate
    with _gate_lock:
        _gate = None


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes | None:
    """All of ``stream``, or None as soon as it is over ``cap`` bytes."""
    parts: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            return b"".join(parts)
        size += len(chunk)
        if size > cap:
            return None
        parts.append(chunk)


async def _feed(stdin: asyncio.StreamWriter, data: bytes) -> None:
    try:
        stdin.write(data)
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # the child died early; its exit status tells why
    finally:
        try:
            stdin.close()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def run_sandbox(
    payload: Mapping[str, Any], limits: ExtractLimits | None = None, *, max_output_bytes: int | None = None
) -> dict[str, Any]:
    """Run the child on ``payload``; its parsed JSON result (``{"ok": ...}``). Raises ExtractionFailed
    for a timeout, a signal, a crash, oversized or malformed output. Bounded by the semaphore."""
    limits = limits or extract_limits()
    cap = EXTRACT_MAX_OUTPUT_BYTES if max_output_bytes is None else max_output_bytes
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    async with _process_gate(limits.extract_concurrency):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limits.extract_wall_seconds
        try:
            proc = await asyncio.create_subprocess_exec(
                *sandbox_command(limits),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=dict(CHILD_ENV),
                cwd="/",
                start_new_session=True,
            )
        except OSError as exc:
            raise SandboxSpawnFailed(exc.errno) from None
        assert proc.stdin is not None and proc.stdout is not None
        killed = False
        try:
            feeder = asyncio.create_task(_feed(proc.stdin, data))
            try:
                out = await asyncio.wait_for(_read_capped(proc.stdout, cap), timeout=max(0.0, deadline - loop.time()))
            except TimeoutError:
                killed = True
                _kill(proc)
                raise ExtractionFailed("extract_timeout", f"over {limits.extract_wall_seconds} s wall time") from None
            finally:
                if not feeder.done():
                    feeder.cancel()
            if out is None:
                killed = True
                _kill(proc)
                raise ExtractionFailed("extract_output_too_large", f"output over {cap} bytes")
            try:
                returncode = await asyncio.wait_for(proc.wait(), timeout=max(0.0, deadline - loop.time()))
            except TimeoutError:
                killed = True
                _kill(proc)
                raise ExtractionFailed("extract_timeout", f"over {limits.extract_wall_seconds} s wall time") from None
        finally:
            if proc.returncode is None:
                _kill(proc)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:  # pragma: no cover - a SIGKILLed child always exits
                    pass
    return _outcome(returncode, out, killed)


def _outcome(returncode: int, out: bytes, killed: bool) -> dict[str, Any]:
    if returncode < 0:
        sig = -returncode
        if sig == signal.SIGXCPU:
            raise ExtractionFailed("extract_timeout", "CPU limit")
        if sig == signal.SIGKILL and not killed:
            raise ExtractionFailed("extract_memory", "killed")
        raise ExtractionFailed("extract_failed", f"signal {sig}")
    if returncode == MEMORY_EXIT:
        raise ExtractionFailed("extract_memory", "out of memory")
    if returncode != 0:
        raise ExtractionFailed("extract_failed", f"exit status {returncode}")
    try:
        result = json.loads(out)
    except (ValueError, UnicodeDecodeError):
        raise ExtractionFailed("extract_failed", "malformed result") from None
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        raise ExtractionFailed("extract_failed", "malformed result")
    return result


# ------------------------------------------------------------------------------- the result


def _str(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ExtractionFailed("extract_failed", f"malformed result: {name}")
    return value


def _int(value: Any, name: str, *, optional: bool = False, minimum: int = 0) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ExtractionFailed("extract_failed", f"malformed result: {name}")
    return value


def _code_ref(raw: Any) -> CodeRef | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ExtractionFailed("extract_failed", "malformed result: code")
    return CodeRef(
        language=_str(raw.get("language"), "code.language") or "",
        path=_str(raw.get("path"), "code.path") or "",
        start_line=_int(raw.get("start_line"), "code.start_line", minimum=1) or 1,
        end_line=_int(raw.get("end_line"), "code.end_line", minimum=1) or 1,
        symbol=_str(raw.get("symbol"), "code.symbol", optional=True),
        kind=_str(raw.get("kind"), "code.kind", optional=True) or "code",
    )


def _section(raw: Any) -> Section:
    if not isinstance(raw, dict):
        raise ExtractionFailed("extract_failed", "malformed result: section")
    path = raw.get("heading_path")
    if not isinstance(path, list) or not all(isinstance(h, str) for h in path):
        raise ExtractionFailed("extract_failed", "malformed result: heading_path")
    return Section(
        heading_path=tuple(path),
        heading=_str(raw.get("heading"), "heading") or "",
        level=_int(raw.get("level"), "level") or 0,
        text=_str(raw.get("text"), "text") or "",
        anchor=_str(raw.get("anchor"), "anchor", optional=True),
        page=_int(raw.get("page"), "page", optional=True, minimum=1),
        page_end=_int(raw.get("page_end"), "page_end", optional=True, minimum=1),
        condition=_str(raw.get("condition"), "condition", optional=True),
        code=_code_ref(raw.get("code")),
    )


def parse_result(result: Mapping[str, Any]) -> SandboxResult:
    """The child's successful result as a SandboxResult; its failure as ExtractionFailed(code)."""
    if not result.get("ok"):
        code = result.get("code")
        detail = result.get("detail")
        raise ExtractionFailed(
            code if isinstance(code, str) else "extract_failed", detail if isinstance(detail, str) else ""
        )
    raw = result.get("document")
    if not isinstance(raw, dict) or not isinstance(raw.get("sections"), list):
        raise ExtractionFailed("extract_failed", "malformed result: document")
    warnings = raw.get("warnings") or []
    if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
        raise ExtractionFailed("extract_failed", "malformed result: warnings")
    document = Document(
        title=_str(raw.get("title"), "title") or "",
        source_url=_str(raw.get("source_url"), "source_url", optional=True),
        format=_str(raw.get("format"), "format") or "",
        sections=tuple(_section(s) for s in raw["sections"]),
        extracted_chars=_int(raw.get("extracted_chars"), "extracted_chars") or 0,
        warnings=tuple(warnings),
    )
    image_only = result.get("image_only_pages") or []
    if not isinstance(image_only, list):
        raise ExtractionFailed("extract_failed", "malformed result: image_only_pages")
    return SandboxResult(
        document=document,
        pages=_int(result.get("pages"), "pages", optional=True),
        image_only_pages=tuple(_int(p, "image_only_pages", minimum=1) or 1 for p in image_only),
    )


async def extract_in_sandbox(
    extractor: str,
    content: str,
    *,
    title: str = "",
    source_url: str | None = None,
    options: Mapping[str, Any] | None = None,
    max_tokens: int = 512,
    overlap_tokens: int = 0,
    code: Mapping[str, Any] | None = None,
    limits: ExtractLimits | None = None,
) -> SandboxResult:
    """Extract one document in the sandbox. ``content`` is the stored text (base64 for a PDF);
    ``options`` the knowledge base's ``chunking.extract`` as JSON. Raises ExtractionFailed."""
    limits = limits or extract_limits()
    payload = {
        "extractor": extractor,
        "content": content,
        "title": title,
        "source_url": source_url,
        "options": dict(options) if options is not None else None,
        "max_tokens": int(max_tokens),
        "overlap_tokens": int(overlap_tokens),
        "code": dict(code) if code is not None else None,
        # The child reads no environment: the limits it applies itself come in the payload.
        "limits": {"max_pdf_pages": limits.max_pdf_pages, "max_structured_bytes": limits.max_structured_bytes},
    }
    return parse_result(await run_sandbox(payload, limits))
