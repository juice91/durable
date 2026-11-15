"""The extraction sandbox's child entry point (RAG P1, task P1.A3): ``python -I -B -m eap_knowledge.sandbox``.

The worker (``eap_worker.extract_sandbox``) starts this module in a disposable child process with an
empty environment, writes ONE JSON payload to its stdin and reads ONE JSON result from its stdout.
It reads no environment and writes nothing else.

Before anything else (before the payload is read and before any parser is imported) ``main``:

1. sets its own resource limits from its arguments (``--cpu SECONDS --memory MB``), before it reads
   the payload or imports a parser module (only the ``eap_knowledge`` package itself, with
   ``markdown_it`` and ``pydantic``, is loaded by then; no input has been seen): ``RLIMIT_CPU``
   (soft = SECONDS, hard one second more), ``RLIMIT_FSIZE`` 0, ``RLIMIT_NOFILE`` 32,
   ``RLIMIT_CORE`` 0 and, on Linux only, ``RLIMIT_AS`` (macOS has no working one). Soft and hard
   limits are both lowered, so a parser cannot raise them again.
2. installs an audit hook (``sys.addaudithook``, which cannot be removed) that refuses, with
   PermissionError (``_REFUSED_EVENTS``, ``_REFUSED_IMPORTS``): creating a socket and every resolver
   call (``getaddrinfo``, ``gethostbyname``, ``gethostbyaddr``, ``getnameinfo``, ...); starting a
   process (``subprocess``, ``os.exec*``, ``os.system``, ``os.posix_spawn``, ``os.spawn*``,
   ``os.fork``, ``pty``; ``_posixsubprocess``, whose ``fork_exec`` has no audit event, cannot be
   imported); signalling any process (``os.kill``); native loaders (``ctypes``, ``sqlite3`` and its
   ``load_extension``); any ``open`` for writing, appending, creating or truncating; and file-system
   changes outside ``open`` (``remove``, ``rename``, ``mkdir``, ``rmdir``, ``symlink``, ``link``,
   ``chmod``, ``chown``, ``truncate``, ``utime``, ``shutil.*``; ``mkfifo``/``mknod`` are removed).

This is defence in depth inside the interpreter, not an OS sandbox: native code in a parser could
still bypass it. Before Pillow or pypdfium2 (native decoders) land in part 2, OS-level network and
file isolation is the gate (see the RAG P1 plan, P1.B1).

Payload::

    {"extractor": "pdf" | "rst" | "yaml" | "json" | "markdown-doxygen",
     "content": "<text, or base64 for pdf>",
     "title": "...", "source_url": "..." | null,
     "options": {<chunking.extract>} | null,
     "max_tokens": 512,
     "limits": {"max_pdf_pages": 2000, "max_structured_bytes": 2097152}}

Result: ``{"ok": true, "document": {...}, "pages"?: N, "image_only_pages"?: [n, ...]}`` or
``{"ok": false, "code": "<extraction error code>", "detail": "<for logs; never document text>"}``.
An unknown exception is ``extract_failed`` with its type name only; MemoryError is ``extract_memory``
(exit status MEMORY_EXIT when even the result cannot be written).

Test hooks (``"eap_test"`` in the payload; the worker never sets it from document data): ``env``
returns the child's environment, ``write`` tries to create, append to or truncate a file (the audit
hook refuses it), ``connect`` tries a TCP connection, ``probe`` runs one named operation from
``_PROBES`` (sockets, resolvers, process starts, signals, native loaders, file-system changes; all
refused),
``output`` writes N bytes, ``sleep`` and ``spin`` wait or burn CPU for N seconds, ``alloc`` allocates
N MiB, ``crash`` exits hard with CRASH_EXIT (never SIGABRT, which would make the macOS crash
reporter write a report).
"""

from __future__ import annotations

import base64
import binascii
import errno
import json
import os
import sys
import time
from dataclasses import asdict
from typing import Any

MEMORY_EXIT = 75
CRASH_EXIT = 70  # the `crash` test hook: a hard exit without SIGABRT (no macOS crash report)
NOFILE_LIMIT = 32
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_REFUSED_EVENTS = frozenset({
    # Network: creating a socket at all, plus every resolver call (a DNS lookup leaks data).
    "socket.__new__", "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto",
    "socket.sendmsg", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
    "socket.gethostname", "socket.sethostname", "socket.getservbyname", "socket.getservbyport",
    # Processes and signals (the child may not signal the worker, which runs as the same user).
    "subprocess.Popen", "os.exec", "os.system", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
    "pty.spawn", "os.startfile", "os.kill", "os.killpg", "signal.pthread_kill",
    # Native code.
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.cdata", "sqlite3.connect", "sqlite3.enable_load_extension",
    "sqlite3.load_extension",
    # File-system changes that do not go through `open`.
    "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.symlink", "os.link", "os.chmod", "os.chown",
    "os.chflags", "os.lchflags", "os.truncate", "os.utime", "os.setxattr", "os.removexattr",
    "os.mkfifo", "os.mknod", "os.chdir", "os.putenv", "os.unsetenv",
    "shutil.rmtree", "shutil.move", "shutil.copyfile", "shutil.copytree", "shutil.copymode",
    "shutil.copystat", "shutil.chown", "shutil.make_archive", "shutil.unpack_archive",
})
# Modules whose (fresh) import is refused: native-code loaders and multiprocessing. None of them is
# loaded when the hook is installed (a test checks that).
_REFUSED_IMPORTS = frozenset({"_ctypes", "ctypes", "_sqlite3", "sqlite3", "multiprocessing", "_multiprocessing", "pty"})
# `_posixsubprocess.fork_exec` starts a process WITHOUT an audit event. pypdf imports `subprocess`
# (hence `_posixsubprocess`) at import time, so the module cannot be refused: install_audit_hook
# imports it first and replaces `fork_exec` with a refusal, and any fresh load of the extension
# (which would bring the original function back) is refused by the hook.
_REFUSED_FRESH_LOADS = frozenset({"_posixsubprocess"})


def _refused_fork_exec(*args: Any, **kwargs: Any) -> Any:
    raise PermissionError(errno.EACCES, "refused by the extraction sandbox: fork_exec")
_DETAIL_CHARS = 200


def set_limits(cpu_seconds: int, memory_mb: int) -> None:
    """Lower this process's resource limits (soft and hard); see the module docstring."""
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (NOFILE_LIMIT, NOFILE_LIMIT))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform.startswith("linux"):
        size = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (size, size))


def _writes(mode: Any, flags: Any) -> bool:
    if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if event in _REFUSED_EVENTS:
        raise PermissionError(errno.EACCES, f"refused by the extraction sandbox: {event}")
    if event == "open" and len(args) >= 3 and _writes(args[1], args[2]):
        raise PermissionError(errno.EACCES, "refused by the extraction sandbox: open for writing")
    if event == "import" and args and isinstance(args[0], str):
        top = args[0].split(".")[0]
        if top in _REFUSED_IMPORTS or top in _REFUSED_FRESH_LOADS:
            raise PermissionError(errno.EACCES, f"refused by the extraction sandbox: import {args[0]}")


def install_audit_hook() -> None:
    """Install the hook (it cannot be removed). Refused modules already imported would bypass the
    import refusal, so they are dropped from sys.modules first (none is, in practice)."""
    for name in list(sys.modules):
        if name.split(".")[0] in _REFUSED_IMPORTS:
            del sys.modules[name]
    import _posixsubprocess
    import subprocess

    _posixsubprocess.fork_exec = _refused_fork_exec
    subprocess._fork_exec = _refused_fork_exec  # type: ignore[attr-defined]
    for name in ("mkfifo", "mknod"):  # no audit event in every Python version: remove them
        for module in (os, sys.modules.get("posix")):
            if module is not None and hasattr(module, name):
                delattr(module, name)
    sys.addaudithook(_audit)


def _parse_args(argv: list[str]) -> tuple[int, int]:
    cpu, memory = 60, 1024
    for flag, value in zip(argv[::2], argv[1::2]):
        if flag == "--cpu":
            cpu = int(value)
        elif flag == "--memory":
            memory = int(value)
    return cpu, memory


def _document_json(document: Any) -> dict[str, Any]:
    out = asdict(document)
    out["sections"] = [
        {k: v for k, v in section.items() if v is not None or k == "anchor"} for section in out["sections"]
    ]
    return out


def _options(raw: Any) -> Any:
    from eap_schemas.knowledge import ExtractConfig

    return None if raw is None else ExtractConfig.model_validate(raw)


def extract_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one extraction; raises ExtractionFailed (or anything else, mapped by ``main``)."""
    from eap_knowledge.errors import ExtractionFailed

    extractor = payload.get("extractor")
    content = payload.get("content")
    if not isinstance(content, str):
        raise ExtractionFailed("extract_failed", "content is not a string")
    title = payload.get("title") or ""
    source_url = payload.get("source_url") or None
    options = _options(payload.get("options"))
    limits = payload.get("limits") or {}
    max_tokens = int(payload.get("max_tokens") or 512)
    result: dict[str, Any] = {"ok": True}
    if extractor == "pdf":
        from eap_knowledge.formats import has_pdf_signature
        from eap_knowledge.formats.pdf import DEFAULT_MAX_PAGES, extract_pdf

        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise ExtractionFailed("extract_failed", "the stored PDF is not valid base64") from None
        if not has_pdf_signature(data):
            raise ExtractionFailed("unsupported_content", "no %PDF- signature")
        pdf = extract_pdf(
            data, options, title=title, source_url=source_url,
            max_pages=int(limits.get("max_pdf_pages") or DEFAULT_MAX_PAGES),
        )
        result.update(document=_document_json(pdf.document), pages=pdf.pages, image_only_pages=list(pdf.image_only_pages))
    elif extractor == "rst":
        from eap_knowledge.formats.rst import extract_rst

        result["document"] = _document_json(extract_rst(content, options, title=title, source_url=source_url))
    elif extractor == "yaml":
        from eap_knowledge.formats.structured import DEFAULT_STRUCTURED_CAP, extract_yaml

        cap = int(limits.get("max_structured_bytes") or DEFAULT_STRUCTURED_CAP)
        result["document"] = _document_json(
            extract_yaml(content, title=title, source_url=source_url, max_tokens=max_tokens, structured_cap=cap)
        )
    elif extractor == "json":
        from eap_knowledge.formats.structured import extract_json

        result["document"] = _document_json(
            extract_json(content, title=title, source_url=source_url, max_tokens=max_tokens)
        )
    elif extractor == "markdown-doxygen":
        from eap_knowledge.formats.doxygen import extract_doxygen_markdown

        result["document"] = _document_json(extract_doxygen_markdown(content, title=title, source_url=source_url))
    else:
        raise ExtractionFailed("extract_failed", "unknown extractor")
    return result


def _test_hook(payload: dict[str, Any]) -> dict[str, Any]:
    mode, arg = payload["eap_test"], payload.get("arg")
    if mode == "env":
        return {"ok": True, "env": dict(os.environ)}
    if mode == "write":  # "how": "open" (open(path, "wb")), "append", "os_open" (O_CREAT), "truncate" (O_TRUNC)
        how = payload.get("how", "open")
        try:
            if how == "open":
                with open(str(arg), "wb") as handle:
                    handle.write(b"x" * 1024)
            elif how == "append":
                with open(str(arg), "ab") as handle:
                    handle.write(b"x")
            elif how == "os_open":
                os.close(os.open(str(arg), os.O_WRONLY | os.O_CREAT, 0o600))
            else:
                os.close(os.open(str(arg), os.O_RDONLY | os.O_TRUNC))
            return {"ok": True, "wrote": True}
        except OSError as exc:
            return {"ok": True, "wrote": False, "errno": exc.errno}
    if mode == "connect":  # arg: [host, port]
        import socket

        try:
            socket.create_connection((str(arg[0]), int(arg[1])), timeout=2).close()
            return {"ok": True, "connected": True}
        except OSError as exc:
            return {"ok": True, "connected": False, "error": type(exc).__name__, "errno": exc.errno}
    if mode == "probe":  # arg: an operation name from _PROBES; "done": it ran, else the refusal
        try:
            _PROBES[str(arg)](payload.get("path"))
            return {"ok": True, "done": True}
        except Exception as exc:  # noqa: BLE001 - the probe reports what refused it
            return {"ok": True, "done": False, "error": type(exc).__name__}
    if mode == "output":
        sys.stdout.buffer.write(b"x" * int(arg))
        sys.stdout.buffer.flush()
        return {"ok": True}
    if mode == "sleep":
        time.sleep(float(arg))
        return {"ok": True}
    if mode == "spin":
        end = time.monotonic() + float(arg)
        while time.monotonic() < end:
            pass
        return {"ok": True}
    if mode == "alloc":
        block = bytearray(int(arg) * 1024 * 1024)
        return {"ok": True, "allocated": len(block)}
    if mode == "crash":
        os._exit(CRASH_EXIT)
    return {"ok": False, "code": "extract_failed", "detail": "unknown test hook"}


def _probe_socket(path: Any) -> None:
    import socket

    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).close()


def _probe_resolve(name: str) -> Any:
    def go(path: Any) -> None:
        import socket

        {
            "gethostbyname": lambda: socket.gethostbyname("localhost"),
            "gethostbyname_ex": lambda: socket.gethostbyname_ex("localhost"),
            "gethostbyaddr": lambda: socket.gethostbyaddr("127.0.0.1"),
            "getnameinfo": lambda: socket.getnameinfo(("127.0.0.1", 80), 0),
            "getaddrinfo": lambda: socket.getaddrinfo("localhost", 80),
        }[name]()

    return go


def _probe_fork_exec(path: Any) -> None:
    import _posixsubprocess

    # The arguments do not matter: the refusal comes first (unhooked, this TypeErrors).
    _posixsubprocess.fork_exec([b"/bin/echo"], [b"/bin/echo"], True, (), None, None, -1, -1, -1, -1, -1, -1,
                               -1, -1, False, False, -1, None, None, None, -1, None, False)


def _probe_fork_exec_reload(path: Any) -> None:
    import importlib.util
    import sysconfig
    from pathlib import Path

    so = next((Path(sysconfig.get_path("stdlib")) / "lib-dynload").glob("_posixsubprocess*"))
    spec = importlib.util.spec_from_file_location("_posixsubprocess", so)
    importlib.util.module_from_spec(spec)  # a fresh copy would have the original fork_exec


def _probe_fs(name: str) -> Any:
    def go(path: Any) -> None:
        import shutil

        p = str(path)
        {
            "remove": lambda: os.remove(p),
            "rename": lambda: os.rename(p, p + ".moved"),
            "mkdir": lambda: os.mkdir(p + ".dir"),
            "rmdir": lambda: os.rmdir(p),
            "symlink": lambda: os.symlink(p, p + ".link"),
            "link": lambda: os.link(p, p + ".hard"),
            "chmod": lambda: os.chmod(p, 0o777),
            "truncate": lambda: os.truncate(p, 0),
            "utime": lambda: os.utime(p, (0, 0)),
            "rmtree": lambda: shutil.rmtree(p),
            "mkfifo": lambda: os.mkfifo(p + ".fifo"),
        }[name]()

    return go


def _probe_sqlite(path: Any) -> None:
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.enable_load_extension(True)


_PROBES: dict[str, Any] = {
    "socket": _probe_socket,
    **{f"resolve:{n}": _probe_resolve(n) for n in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo", "getaddrinfo")},
    "subprocess": lambda path: __import__("subprocess").run(["/bin/echo", "x"], check=False),
    "fork_exec": _probe_fork_exec,
    "fork_exec_reload": _probe_fork_exec_reload,
    "fork": lambda path: os.fork(),
    "posix_spawn": lambda path: os.posix_spawn("/bin/echo", ["echo", "x"], {}),
    "system": lambda path: os.system("/bin/echo x"),
    "execv": lambda path: os.execv("/bin/echo", ["echo", "x"]),
    "kill_parent": lambda path: os.kill(os.getppid(), 0),
    "kill_self": lambda path: os.kill(os.getpid(), 0),
    "ctypes": lambda path: __import__("ctypes"),
    "sqlite": _probe_sqlite,
    "pty": lambda path: __import__("pty"),
    **{f"fs:{n}": _probe_fs(n) for n in (
        "remove", "rename", "mkdir", "rmdir", "symlink", "link", "chmod", "truncate", "utime", "rmtree", "mkfifo")},
}


def _failure(code: str, detail: str = "") -> dict[str, Any]:
    return {"ok": False, "code": code, "detail": detail[:_DETAIL_CHARS]}


def run(raw: bytes) -> dict[str, Any]:
    """The result for one raw payload (never raises, except MemoryError's last resort in ``main``)."""
    from eap_knowledge.errors import ExtractionFailed

    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return _failure("extract_failed", "payload is not an object")
        if "eap_test" in payload:
            return _test_hook(payload)
        return extract_payload(payload)
    except ExtractionFailed as exc:
        return _failure(exc.code, exc.detail)
    except MemoryError:
        return _failure("extract_memory")
    except RecursionError:
        return _failure("extract_limit", "nesting too deep")
    except Exception as exc:  # noqa: BLE001 - any other failure: its type only, never its text
        return _failure("extract_failed", type(exc).__name__)


def main(argv: list[str] | None = None) -> int:
    cpu, memory = _parse_args(sys.argv[1:] if argv is None else argv)
    set_limits(cpu, memory)
    install_audit_hook()
    try:
        raw = sys.stdin.buffer.read()
        result = run(raw)
        del raw
        out = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
        return 0
    except MemoryError:
        os._exit(MEMORY_EXIT)


if __name__ == "__main__":
    sys.exit(main())
