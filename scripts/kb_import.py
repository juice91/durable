"""Import a directory tree into a knowledge base through the product API (RAG P1, task P1.A6).

    uv run python scripts/kb_import.py <dir> --kb <kb_id> [options]

Every supported file under <dir> becomes one document (the format registry in
`eap_knowledge.formats` maps extensions to canonical MIME types; other files are skipped). The
document key is the file's path relative to <dir> with `/` written as `~` (the API's keys are one
URL path segment), optionally prefixed by `--key-prefix`. Text formats are sent as `content`, binary
ones (PDF) as `content_base64`. The API decides what changed: an identical upload is a no-op
(`unchanged`), a changed one starts a new version and its ingest run.

The bearer token comes from the EAP_API_TOKEN environment variable or from `--token-file`, never
from the command line, and is never printed. See docs/knowledge-import.md.

`--delete-missing` is fail-safe. It needs `--key-prefix` (or an explicit `--delete-scope all`).
Candidates are documents under the prefix that this CLI stamped (metadata `import_tool:
kb_import`, `import_prefix`), whose file is gone and whose path would pass this run's filters.
Deletes run after the uploads and are skipped when any file failed or any directory could not be
read. A deletion larger than `--max-delete` documents (default 50) or `--max-delete-percent` of the
scope (default 20 %; the two rules are independent) is refused before anything is uploaded, in a
dry run too, unless `--yes-delete-all`.

All HTTP goes through an `ApiClient` (`request(method, path, *, json=None, params=None) ->
ApiResponse`), so tests drive the importer with an in-memory fake and no server.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import json
import os
import random
import re
import stat
import string
import sys
import time
import unicodedata
import urllib.parse
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from eap_knowledge.formats import format_for_filename
from eap_schemas.knowledge import MAX_DOCUMENTS_PER_KNOWLEDGE_BASE

DEFAULT_API_URL = "http://localhost:8000/api/v1"
TOKEN_ENV = "EAP_API_TOKEN"
API_URL_ENV = "EAP_API_URL"
MAX_KEY_LENGTH = 256  # eap_api.schemas.MAX_DOCUMENT_KEY_LENGTH
MAX_TITLE_LENGTH = 500  # eap_api.schemas.MAX_DOCUMENT_TITLE_LENGTH
MAX_METADATA_KEYS = 16  # eap_schemas.knowledge (validate_kb_metadata)
MAX_CONCURRENCY = 32
RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_ATTEMPTS = 6  # the first try plus five retries
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
RETRY_AFTER_MAX_SECONDS = 120.0
# The largest per-format cap the API can be configured with (EAP_KB_MAX_BINARY_BYTES' maximum):
# anything bigger is refused locally, before it is read.
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_DELETE = 50
DEFAULT_MAX_DELETE_PERCENT = 20.0
KEY_SEPARATOR = "~"
# Stamped into every document's metadata: --delete-missing (scope `prefix`) only deletes documents
# this CLI created under the same key prefix, never Studio uploads or another tree's documents.
IMPORT_TOOL_KEY = "import_tool"
IMPORT_TOOL_VALUE = "kb_import"
IMPORT_PREFIX_KEY = "import_prefix"
STAMP_KEYS = (IMPORT_TOOL_KEY, IMPORT_PREFIX_KEY)
MAX_MAP_METADATA_KEYS = MAX_METADATA_KEYS - len(STAMP_KEYS)
_SHORTENED_KEY = re.compile(r"~[0-9a-f]{12}\Z")
MAX_REASON_CHARS = 300
TEMPLATE_FIELDS = ("path", "path_no_ext", "dir", "name", "stem", "ext", "key")

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2


class UsageError(Exception):
    """A bad option, map file, token or knowledge base: the import stops before uploading (exit 2)."""


class DeleteRefused(UsageError):
    """--delete-missing over its threshold: nothing was uploaded or deleted (exit 2). Carries the
    report, so the CLI prints exactly what would have been deleted."""

    def __init__(self, report: "Report") -> None:
        self.report = report
        super().__init__(report.delete_refused or "--delete-missing refused")


# ------------------------------------------------------------------------------------ HTTP client


@dataclasses.dataclass(frozen=True)
class ApiResponse:
    status: int
    body: Any  # parsed JSON, or None for an empty body
    # Response headers, lower-case names (only `retry-after` is read). Optional for fake clients.
    headers: Mapping[str, str] = dataclasses.field(default_factory=dict)


class ApiClient(Protocol):
    """What the importer needs from HTTP. `path` is relative to the API base (`/knowledge-bases/...`,
    already percent-encoded); a transport failure raises `TransportError`."""

    def request(
        self, method: str, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None
    ) -> ApiResponse: ...


class TransportError(Exception):
    """The API could not be reached (connection refused, timeout, ...). Retried, then fatal."""


class HttpxClient:
    """The real client: httpx (the repo installs it as `httpx2`), one pooled connection set shared
    by the worker threads. The token is only ever placed in the Authorization header."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 300.0) -> None:
        try:
            import httpx2 as httpx  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - an environment with upstream httpx
            import httpx  # type: ignore[import-not-found,no-redef]
        self._httpx = httpx
        self._token = token
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": bearer(token)},
            timeout=timeout,
            follow_redirects=False,
        )

    def request(
        self, method: str, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None
    ) -> ApiResponse:
        try:
            response = self._client.request(method, path, json=json, params=params)
        except Exception as exc:  # noqa: BLE001 - HTTPError, InvalidURL, StreamError, ...: all transport
            # The message names the URL at most; scrub the token anyway, in case a proxy echoes it.
            raise TransportError(redact(f"{type(exc).__name__}: {exc}", self._token)) from None
        raw = response.content
        body: Any = None
        if raw:
            try:
                body = response.json()
            except ValueError:
                body = {"detail": raw[:MAX_REASON_CHARS].decode("utf-8", "replace")}
        headers = {"retry-after": response.headers["retry-after"]} if "retry-after" in response.headers else {}
        return ApiResponse(response.status_code, body, headers)

    def close(self) -> None:
        self._client.close()


def bearer(token: str) -> str:
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


def redact(text: str, token: str | None) -> str:
    if not token:
        return text
    for secret in sorted({token, bearer(token), bearer(token).split(" ", 1)[1]}, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "***")
    return text


def read_token(token_file: Path | None, environ: dict[str, str] | None = None) -> str | None:
    """The bearer token: `--token-file` (first non-empty line) if given, else EAP_API_TOKEN."""
    env = os.environ if environ is None else environ
    if token_file is not None:
        try:
            lines = token_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            # The path is not echoed: a token pasted as the path would be printed.
            raise UsageError(f"cannot read the --token-file (path not shown): {exc.strerror or type(exc).__name__}") from None
        token = next((line.strip() for line in lines if line.strip()), "")
        if not token:
            raise UsageError("the --token-file is empty")
        return token
    token = (env.get(TOKEN_ENV) or "").strip()
    return token or None


# --------------------------------------------------------------------------------------- globbing


def glob_regex(pattern: str) -> re.Pattern[str]:
    """A glob over POSIX relative paths: `**` any number of path segments (including none), `*` and
    `?` within one segment, `[...]` a character class. A pattern without `/` matches the file name
    in any directory (`*.pdf`)."""
    pattern = pattern.strip().lstrip("/")
    if "/" not in pattern:
        pattern = "**/" + pattern
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            end = pattern.find("]", i + 2 if pattern[i + 1 : i + 2] in ("!", "]") else i + 1)
            if end < 0:
                out.append(re.escape(c))
                i += 1
                continue
            body = pattern[i + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


class Globs:
    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns = [p for p in patterns if p.strip()]
        self._compiled = [glob_regex(p) for p in self.patterns]

    def __bool__(self) -> bool:
        return bool(self._compiled)

    def first(self, rel_path: str) -> int | None:
        return next((i for i, rx in enumerate(self._compiled) if rx.match(rel_path)), None)

    def matches(self, rel_path: str) -> bool:
        return self.first(rel_path) is not None


# ----------------------------------------------------------------------------------- metadata map


@dataclasses.dataclass(frozen=True)
class MetadataRule:
    glob: str
    metadata: dict[str, str | int | float | bool]


class MetadataMap:
    """Ordered glob -> flat metadata rules; the FIRST matching glob wins (rules are not merged)."""

    def __init__(self, rules: Sequence[MetadataRule]) -> None:
        self.rules = list(rules)
        self._globs = Globs(r.glob for r in self.rules)

    def lookup(self, rel_path: str) -> dict[str, Any] | None:
        index = self._globs.first(rel_path)
        return None if index is None else dict(self.rules[index].metadata)


def _flat_metadata(where: str, value: Any) -> dict[str, str | int | float | bool]:
    if not isinstance(value, dict):
        raise UsageError(f"{where}: the metadata must be a mapping of key: value")
    if len(value) > MAX_MAP_METADATA_KEYS:
        raise UsageError(
            f"{where}: at most {MAX_MAP_METADATA_KEYS} metadata keys (the API allows {MAX_METADATA_KEYS};"
            f" {', '.join(STAMP_KEYS)} are stamped by the importer)"
        )
    out: dict[str, str | int | float | bool] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise UsageError(f"{where}: metadata keys must be non-empty strings")
        if key == "eap":
            raise UsageError(f"{where}: the metadata key 'eap' is reserved")
        if key in STAMP_KEYS:
            raise UsageError(f"{where}: the metadata key {key!r} is reserved (the importer stamps it)")
        if isinstance(item, float) and item != item:
            raise UsageError(f"{where}: {key!r} is NaN")
        if not isinstance(item, (str, int, float, bool)):
            raise UsageError(
                f"{where}: {key!r} must be a string, number or boolean (flat values only), not {type(item).__name__}"
            )
        out[key] = item
    return out


def load_metadata_map(path: Path) -> MetadataMap:
    """Read a map file (YAML or JSON): either a mapping `glob: {key: value, ...}` in file order, or
    `{rules: [{match: glob, metadata: {...}}, ...]}`. Values must be flat (string, number, boolean)."""
    import yaml

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read the metadata map {path}: {exc.strerror or exc}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise UsageError(f"the metadata map {path} is not valid YAML: {exc}") from None
    if data is None:
        return MetadataMap([])
    rules: list[MetadataRule] = []
    if isinstance(data, dict) and set(data) == {"rules"} and isinstance(data["rules"], list):
        for n, rule in enumerate(data["rules"], 1):
            where = f"{path}: rule {n}"
            if not isinstance(rule, dict) or not isinstance(rule.get("match"), str) or not rule["match"].strip():
                raise UsageError(f"{where}: each rule needs `match: <glob>` and `metadata: {{...}}`")
            rules.append(MetadataRule(rule["match"], _flat_metadata(where, rule.get("metadata"))))
    elif isinstance(data, dict):
        for glob, metadata in data.items():
            if not isinstance(glob, str) or not glob.strip():
                raise UsageError(f"{path}: every glob must be a non-empty string")
            rules.append(MetadataRule(glob, _flat_metadata(f"{path}: {glob!r}", metadata)))
    else:
        raise UsageError(f"{path}: expected a mapping of glob: metadata (or rules: [...])")
    return MetadataMap(rules)


# -------------------------------------------------------------------------------- source-URL template


def check_template(template: str) -> None:
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(template) if f is not None]
    except ValueError as exc:
        raise UsageError(f"--source-url-template is not a valid template: {exc}") from None
    for field in fields:
        if field not in TEMPLATE_FIELDS:
            raise UsageError(
                f"--source-url-template: unknown placeholder {{{field}}}; use {', '.join('{' + f + '}' for f in TEMPLATE_FIELDS)}"
            )
    scheme = template.split("://", 1)[0].lower() if "://" in template else ""
    if scheme not in ("http", "https"):
        raise UsageError("--source-url-template must start with http:// or https://")


def render_source_url(template: str, rel_path: str, key: str) -> str:
    """The template with each placeholder filled in; path values are percent-encoded per segment
    (`/` kept), so a file name with spaces still makes a valid URL."""
    q = lambda s: urllib.parse.quote(s, safe="/")  # noqa: E731
    p = Path(rel_path)
    parent = p.parent.as_posix()
    values = {
        "path": q(rel_path),
        "path_no_ext": q(str(p.with_suffix("").as_posix()) if p.suffix else rel_path),
        "dir": "" if parent == "." else q(parent),
        "name": q(p.name),
        "stem": q(p.stem),
        "ext": q(p.suffix[1:]),
        "key": urllib.parse.quote(key, safe=""),
    }
    return template.format_map(values)


# ------------------------------------------------------------------------------------------ files


@dataclasses.dataclass(frozen=True)
class SourceFile:
    path: Path  # absolute
    rel_path: str  # POSIX, relative to the root
    key: str
    format: str
    mime_type: str
    binary: bool
    # The scanned file's identity: load_file refuses a path that now names a different file (a
    # symlink or file swapped after the scan, possibly pointing outside the tree).
    dev: int = 0
    ino: int = 0


def normalise_prefix(prefix: str) -> str:
    """A key prefix is a whole path segment: `sdk` becomes `sdk~`, so `--delete-missing` under it
    never matches `sdk2~...` documents. Empty stays empty."""
    return prefix if not prefix or prefix.endswith(KEY_SEPARATOR) else prefix + KEY_SEPARATOR


def check_api_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UsageError("--api-url (or $EAP_API_URL) must be an absolute http:// or https:// URL")


def document_key(rel_path: str, prefix: str = "") -> str:
    """`prefix` + the relative path (Unicode NFC, so an NFD checkout from macOS gets the same keys
    as an NFC one from Linux) with `/` as `~`. A key over 256 characters keeps its head and ends in
    `~` plus 12 hex digits of the full key's sha256 (stable across runs)."""
    key = prefix + unicodedata.normalize("NFC", rel_path).replace("/", "~")
    if len(key) > MAX_KEY_LENGTH:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        key = key[: MAX_KEY_LENGTH - 13] + "~" + digest
    return key


def key_problem(key: str) -> str | None:
    """The API's document-key rules (eap_api.schemas.document_key_problem) checked locally."""
    if "\\" in key or any(not ch.isprintable() for ch in key) or key != key.strip():
        return "the document key would contain '\\', a control character or surrounding whitespace"
    return None


@dataclasses.dataclass
class Scan:
    files: list[SourceFile]
    skipped: Counter[str]  # reason -> count (unsupported extension, hidden, excluded, ...)
    problems: list[tuple[str, str]]  # (rel_path, reason): files that cannot become documents
    # The key of EVERY file the walk saw under the root (filtered, hidden, unsupported, failed or
    # not): a document with one of these keys still has its file, so it is never "missing".
    on_disk_keys: set[str] = dataclasses.field(default_factory=set)
    selector: "Selector | None" = None
    # (relative path, reason) of every directory the walk could not list: its files are unknown, so
    # --delete-missing must not run (they would look "missing").
    walk_errors: list[tuple[str, str]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class Selector:
    """This run's file filters, applied to a relative path (also to the path a document key decodes
    to, which decides whether a missing document belongs to this run's scope)."""

    include: Globs
    exclude: Globs
    include_hidden: bool = False

    def why_not(self, rel: str) -> str | None:
        """None when this run would import `rel`, else the skip reason."""
        if not self.include_hidden and any(seg.startswith(".") for seg in rel.split("/")):
            return "hidden"
        if self.include and not self.include.matches(rel):
            return "not included"
        if self.exclude.matches(rel):
            return "excluded"
        if format_for_filename(rel.rsplit("/", 1)[-1]) is None:
            return "unsupported type"
        return None


def scan_tree(
    root: Path,
    *,
    include: Globs,
    exclude: Globs,
    key_prefix: str = "",
    include_hidden: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> Scan:
    """Walk `root` (sorted, symlinked directories not followed, symlinked files only when they stay
    inside the root) and classify each file. Every walked file's key is recorded in `on_disk_keys`.
    Hidden directories are pruned unless `include_hidden` (their paths are never deletion candidates
    then either: `Selector.why_not` says "hidden")."""
    root = root.resolve()
    selector = Selector(include, exclude, include_hidden)
    files: list[SourceFile] = []
    skipped: Counter[str] = Counter()
    problems: list[tuple[str, str]] = []
    on_disk: set[str] = set()
    walk_errors: list[tuple[str, str]] = []

    def walk_error(exc: OSError) -> None:
        where = Path(exc.filename) if exc.filename else root
        try:
            rel_dir = where.relative_to(root).as_posix()
        except ValueError:
            rel_dir = str(where)
        walk_errors.append((rel_dir, exc.strerror or type(exc).__name__))

    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error, followlinks=False):
        dirnames.sort()
        if not include_hidden:
            hidden = [d for d in dirnames if d.startswith(".")]
            if hidden:
                skipped["hidden directory"] += len(hidden)  # counted per directory, not per file
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            on_disk.add(document_key(rel, key_prefix))
            why = selector.why_not(rel)
            if why is not None:
                skipped[why] += 1
                continue
            try:
                st = path.stat()  # follows a symlink
                if path.is_symlink():
                    path.resolve(strict=True).relative_to(root)
            except (OSError, ValueError):
                skipped["symlink outside the tree"] += 1
                continue
            if not stat.S_ISREG(st.st_mode):
                skipped["not a regular file"] += 1
                continue
            spec = format_for_filename(name)
            assert spec is not None  # why_not checked it
            key = document_key(rel, key_prefix)
            problem = key_problem(key)
            if problem is None and st.st_size > max_file_bytes:
                problem = f"the file is too large ({st.st_size} bytes; --max-file-bytes is {max_file_bytes})"
            if problem is not None:
                problems.append((rel, problem))
                continue
            files.append(
                SourceFile(path, rel, key, spec.format, spec.mime_type, spec.binary, st.st_dev, st.st_ino)
            )
    by_key: dict[str, list[SourceFile]] = {}
    for f in files:
        by_key.setdefault(f.key, []).append(f)
    unique: list[SourceFile] = []
    for f in files:
        same = by_key[f.key]
        if len(same) > 1:
            others = ", ".join(o.rel_path for o in same if o is not f)
            problems.append((f.rel_path, f"its document key {f.key!r} is also the key of {others}"))
        else:
            unique.append(f)
    return Scan(unique, skipped, problems, on_disk, selector, sorted(walk_errors))


@dataclasses.dataclass(frozen=True)
class LoadedFile:
    body: dict[str, Any]  # the PUT body without run_id
    content_hash: str  # what the API stores as kb_documents.content_hash


def read_scanned(f: SourceFile, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> bytes:
    """The bytes of the file the scan saw: opened once, then checked by identity (device and inode)
    and size on the open descriptor, so a path swapped after the scan (for a symlink out of the
    tree, or a huge file) is refused instead of read."""
    # O_NONBLOCK: a FIFO swapped in after the scan must not block the open (fstat then rejects it);
    # it has no effect on a regular file's reads.
    fd = os.open(f.path, os.O_RDONLY | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if f.ino and (st.st_dev, st.st_ino) != (f.dev, f.ino):
            raise ValueError("the file changed after the scan (a different file is now at this path)")
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        if st.st_size > max_file_bytes:
            raise ValueError(f"the file is too large ({st.st_size} bytes; --max-file-bytes is {max_file_bytes})")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(max_file_bytes + 1)
        if len(data) > max_file_bytes:
            raise ValueError(f"the file grew past --max-file-bytes ({max_file_bytes}) while it was read")
        return data
    finally:
        os.close(fd)


def load_file(
    f: SourceFile,
    *,
    metadata_map: MetadataMap | None,
    source_url_template: str | None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    key_prefix: str = "",
) -> LoadedFile:
    """Read a file and build its PUT body. Raises ValueError with a reason (not UTF-8 text, empty)."""
    data = read_scanned(f, max_file_bytes)
    if not data:
        raise ValueError("the file is empty")
    body: dict[str, Any] = {"mime_type": f.mime_type, "title": f.rel_path[-MAX_TITLE_LENGTH:]}
    if f.binary:
        body["content_base64"] = base64.b64encode(data).decode("ascii")
        digest = hashlib.sha256(data).hexdigest()
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"not UTF-8 text (invalid byte at offset {exc.start})") from None
        if not text.strip():
            raise ValueError("the file has only whitespace")
        body["content"] = text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if source_url_template:
        body["source_url"] = render_source_url(source_url_template, f.rel_path, f.key)
    metadata = (metadata_map.lookup(f.rel_path) if metadata_map is not None else None) or {}
    body["metadata"] = {**metadata, **stamp(key_prefix)}
    return LoadedFile(body, digest)


def stamp(key_prefix: str) -> dict[str, str]:
    """The metadata every imported document carries (see STAMP_KEYS)."""
    return {IMPORT_TOOL_KEY: IMPORT_TOOL_VALUE, IMPORT_PREFIX_KEY: key_prefix}


def is_ours(document: dict[str, Any], key_prefix: str) -> bool:
    """Whether this CLI created `document` under `key_prefix` (its stamp says so)."""
    metadata = document.get("metadata") or {}
    return metadata.get(IMPORT_TOOL_KEY) == IMPORT_TOOL_VALUE and metadata.get(IMPORT_PREFIX_KEY) == key_prefix


# ----------------------------------------------------------------------------------------- import


@dataclasses.dataclass
class Outcome:
    key: str
    rel_path: str
    result: str  # created | new_version | unchanged | retried | failed (| would_* in a dry run)
    reason: str | None = None
    run_id: str | None = None


@dataclasses.dataclass
class Report:
    kb: str
    dry_run: bool
    outcomes: list[Outcome] = dataclasses.field(default_factory=list)
    formats: Counter[str] = dataclasses.field(default_factory=Counter)
    skipped: Counter[str] = dataclasses.field(default_factory=Counter)
    missing: list[str] = dataclasses.field(default_factory=list)  # KB keys no longer in the tree
    in_scope: int = 0  # the knowledge base's documents in the delete scope
    kept_unverifiable: list[str] = dataclasses.field(default_factory=list)  # shortened keys, file unknown
    kept_foreign: list[str] = dataclasses.field(default_factory=list)  # gone, but not created by this CLI here
    walk_errors: list[tuple[str, str]] = dataclasses.field(default_factory=list)  # unlistable directories
    delete_refused: str | None = None  # why --delete-missing refused (over the threshold)
    deletes_skipped: str | None = None  # why the deletes did not run (failed uploads)
    deleted: list[str] = dataclasses.field(default_factory=list)
    delete_failed: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    unchanged_failed: list[str] = dataclasses.field(default_factory=list)  # unchanged, last ingest failed
    notes: list[str] = dataclasses.field(default_factory=list)

    def counts(self) -> Counter[str]:
        return Counter(o.result for o in self.outcomes)

    @property
    def failed(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.result == "failed"]

    def to_json(self) -> dict[str, Any]:
        return {
            "kb": self.kb,
            "dry_run": self.dry_run,
            "counts": dict(sorted(self.counts().items())),
            "formats": dict(sorted(self.formats.items())),
            "skipped": dict(sorted(self.skipped.items())),
            "documents": [dataclasses.asdict(o) for o in self.outcomes],
            "missing": self.missing,
            "in_scope": self.in_scope,
            "kept_unverifiable": self.kept_unverifiable,
            "kept_foreign": self.kept_foreign,
            "walk_errors": [{"path": p, "reason": r} for p, r in self.walk_errors],
            "delete_refused": self.delete_refused,
            "deletes_skipped": self.deletes_skipped,
            "deleted": self.deleted,
            "delete_failed": [{"key": k, "reason": r} for k, r in self.delete_failed],
            "unchanged_failed": self.unchanged_failed,
            "notes": self.notes,
        }


def _doc_path(kb: str, key: str | None = None) -> str:
    base = f"/knowledge-bases/{urllib.parse.quote(kb, safe='')}"
    return base if key is None else f"{base}/documents/{urllib.parse.quote(key, safe='')}"


def _reason(response: ApiResponse) -> str:
    body = response.body
    detail = body.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    text = f"HTTP {response.status}" + (f": {detail}" if detail else "")
    return text[:MAX_REASON_CHARS]


def retry_after_seconds(response: ApiResponse) -> float | None:
    """The response's Retry-After in seconds (the delta-seconds form; an HTTP date is ignored)."""
    raw = next((v for k, v in (response.headers or {}).items() if k.lower() == "retry-after"), None)
    try:
        value = float(str(raw).strip()) if raw is not None else None
    except ValueError:
        return None
    return None if value is None or value != value or value < 0 else min(value, RETRY_AFTER_MAX_SECONDS)


@dataclasses.dataclass(frozen=True)
class DeleteLimits:
    """When --delete-missing may delete: at most `max_count` documents (None: no count limit) AND at
    most `max_percent` % of the documents in the delete scope (None: no percent limit). The two
    rules are independent: raising one never lifts the other."""

    max_count: int | None = DEFAULT_MAX_DELETE
    max_percent: float | None = DEFAULT_MAX_DELETE_PERCENT

    def refusal(self, deletions: int, in_scope: int) -> str | None:
        if deletions == 0:
            return None
        if self.max_count is not None and deletions > self.max_count:
            return f"{deletions} deletions exceed the limit of {self.max_count} documents"
        if self.max_percent is not None and in_scope and deletions * 100 > self.max_percent * in_scope:
            pct = deletions * 100 / in_scope
            return (
                f"{deletions} deletions are {pct:.0f} % of the {in_scope} documents under the key prefix,"
                f" over the limit of {self.max_percent:g} %"
            )
        return None


class Importer:
    def __init__(
        self,
        client: ApiClient,
        kb: str,
        *,
        concurrency: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = MAX_ATTEMPTS,
        token: str | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if not 1 <= concurrency <= MAX_CONCURRENCY:
            raise UsageError(f"--concurrency must be 1 to {MAX_CONCURRENCY}")
        self.client = client
        self.kb = kb
        self.concurrency = concurrency
        self.sleep = sleep
        self.max_attempts = max_attempts
        self._token = token  # only to scrub messages; never printed
        self._rng = rng or random.Random()

    # -- HTTP with retries
    def backoff(self, attempt: int, response: ApiResponse | None = None) -> float:
        """The wait before retry `attempt + 1`: the server's Retry-After when it sent one (plus up to
        1 s of jitter), else exponential (1, 2, 4, 8, 16 s, capped at 30 s) with "equal jitter" (half
        fixed, half random), so concurrent workers do not retry in lockstep."""
        hinted = retry_after_seconds(response) if response is not None else None
        if hinted is not None:
            return hinted + self._rng.uniform(0, 1)
        delay = min(BACKOFF_BASE_SECONDS * 2**attempt, BACKOFF_MAX_SECONDS)
        return delay / 2 + self._rng.uniform(0, delay / 2)

    def call(self, method: str, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None) -> ApiResponse:
        """One request, retried (up to `max_attempts - 1` times) on 429/502/503/504 and transport
        errors, with `backoff`. A PUT keeps its run_id across attempts, so a retried 503 (enqueue
        failed) re-queues the same run."""
        for attempt in range(self.max_attempts):
            last = attempt == self.max_attempts - 1
            try:
                response = self.client.request(method, path, json=json, params=params)
            except TransportError:
                if last:
                    raise
                self.sleep(self.backoff(attempt))
                continue
            if response.status in RETRY_STATUSES and not last:
                self.sleep(self.backoff(attempt, response))
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def check_kb(self) -> dict[str, Any]:
        response = self.call("GET", _doc_path(self.kb))
        if response.status == 404:
            raise UsageError(f"knowledge base {self.kb!r} not found (for this token's tenant)")
        if response.status == 401:
            raise UsageError("the API refused the token (401); check EAP_API_TOKEN or --token-file")
        if response.status != 200 or not isinstance(response.body, dict):
            raise UsageError(f"cannot read knowledge base {self.kb!r}: {_reason(response)}")
        return response.body

    def list_documents(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 200}
            if after:
                params["after"] = after
            response = self.call("GET", _doc_path(self.kb) + "/documents", params=params)
            if response.status != 200 or not isinstance(response.body, dict):
                raise UsageError(f"cannot list the documents of {self.kb!r}: {_reason(response)}")
            out.extend(response.body.get("items") or [])
            after = response.body.get("next_after")
            if not after:
                return out

    # -- one document
    def upload(self, f: SourceFile, loaded: LoadedFile, *, retry_failed: bool) -> Outcome:
        body = {**loaded.body, "run_id": str(uuid.uuid4())}
        response = self.call("PUT", _doc_path(self.kb, f.key), json=body)
        data = response.body if isinstance(response.body, dict) else {}
        if response.status == 200 and data.get("unchanged"):
            document = data.get("document") or {}
            if document.get("status") == "failed":
                if retry_failed:
                    return self._reingest(f)
                return Outcome(f.key, f.rel_path, "unchanged", reason="last ingest failed", run_id=None)
            return Outcome(f.key, f.rel_path, "unchanged")
        if response.status in (200, 202):
            result = "created" if data.get("created") else "new_version"
            return Outcome(f.key, f.rel_path, result, run_id=data.get("run_id"))
        return Outcome(f.key, f.rel_path, "failed", reason=redact(_reason(response), self._token))

    def _reingest(self, f: SourceFile) -> Outcome:
        """POST reingest. 202: a run started (`retried`). 200 `unchanged` (it became ready since the
        PUT) and 409 (an ingest of it is already queued or running) are not failures: `unchanged`
        with the reason. Anything else fails."""
        response = self.call("POST", _doc_path(self.kb, f.key) + "/reingest", json={"run_id": str(uuid.uuid4())})
        data = response.body if isinstance(response.body, dict) else {}
        if response.status == 202 or (response.status == 200 and data.get("run_id")):
            return Outcome(f.key, f.rel_path, "retried", run_id=data.get("run_id"))
        if response.status == 200:
            return Outcome(f.key, f.rel_path, "unchanged", reason="already ingested; no re-ingest needed")
        if response.status == 409:
            return Outcome(
                f.key, f.rel_path, "unchanged", reason="re-ingest not started: " + redact(_reason(response), self._token)
            )
        return Outcome(f.key, f.rel_path, "failed", reason="reingest " + redact(_reason(response), self._token))

    # -- the whole tree
    def run(
        self,
        scan: Scan,
        *,
        metadata_map: MetadataMap | None = None,
        source_url_template: str | None = None,
        dry_run: bool = False,
        delete_missing: bool = False,
        retry_failed: bool = False,
        key_prefix: str = "",
        online: bool = True,
        delete_limits: DeleteLimits | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        document_cap: int = MAX_DOCUMENTS_PER_KNOWLEDGE_BASE,
        delete_scope: str = "prefix",
    ) -> Report:
        """Plan, then upload, then (with --delete-missing) delete. Raises UsageError, before any
        upload, when the deletion is over `delete_limits` or the knowledge base would pass its
        document cap; a dry run reports both instead."""
        key_prefix = normalise_prefix(key_prefix)
        if delete_missing and delete_scope == "prefix" and not key_prefix:
            raise UsageError(
                "--delete-missing needs --key-prefix, so it can only delete this tree's documents; pass"
                " --delete-scope all to make the whole knowledge base the scope"
            )
        limits = delete_limits or DeleteLimits()
        report = Report(self.kb, dry_run)
        report.walk_errors = list(scan.walk_errors)
        report.skipped.update(scan.skipped)
        report.formats.update(f.format for f in scan.files)
        for rel, reason in scan.problems:
            report.outcomes.append(Outcome(document_key(rel, key_prefix), rel, "failed", reason=reason))

        existing: dict[str, dict[str, Any]] | None = None
        if online:
            self.check_kb()
            # Listed up front: the dry run's estimate, the cap check and the missing documents.
            existing = {d["document_key"]: d for d in self.list_documents()}
        elif not dry_run:
            raise UsageError("no API token: set EAP_API_TOKEN or pass --token-file")
        else:
            report.notes.append("no API token: the dry run counts files only (no change estimate)")

        if existing is not None:
            self._plan(report, scan, existing, key_prefix, limits, delete_missing, document_cap, delete_scope)

        def one(f: SourceFile) -> Outcome:
            try:
                loaded = load_file(
                    f,
                    metadata_map=metadata_map,
                    source_url_template=source_url_template,
                    max_file_bytes=max_file_bytes,
                    key_prefix=key_prefix,
                )
            except (OSError, ValueError) as exc:
                reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
                return Outcome(f.key, f.rel_path, "failed", reason=reason)
            if dry_run:
                return Outcome(f.key, f.rel_path, _dry_result(existing, f, loaded))
            try:
                return self.upload(f, loaded, retry_failed=retry_failed)
            except TransportError as exc:
                return Outcome(f.key, f.rel_path, "failed", reason=str(exc)[:MAX_REASON_CHARS])

        # Bounded: at most `concurrency` files are read and uploaded at a time (pool.map keeps order).
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="kb-import") as pool:
            report.outcomes.extend(pool.map(one, scan.files))

        for o in report.outcomes:
            if o.result == "unchanged" and o.reason == "last ingest failed":
                report.unchanged_failed.append(o.key)

        if delete_missing and report.missing and report.delete_refused is None:
            if not scan.files and not scan.problems:
                report.notes.append("--delete-missing ignored: the tree produced no documents")
            elif report.walk_errors:
                # A directory that could not be listed hides its files: they would look "missing".
                report.deletes_skipped = (
                    f"{len(report.walk_errors)} director(ies) could not be read, so the {len(report.missing)}"
                    " deletion(s) were skipped; fix the permissions (or --exclude them) and re-run"
                )
            elif report.failed:
                # Deletes run only after every upload succeeded: a half-failed import keeps the old
                # documents until a clean re-run.
                report.deletes_skipped = (
                    f"{len(report.failed)} file(s) failed, so the {len(report.missing)} deletion(s) were skipped;"
                    " fix the failures and re-run"
                )
            elif not dry_run:
                self._delete(report)
        return report

    def _plan(
        self,
        report: Report,
        scan: Scan,
        existing: dict[str, dict[str, Any]],
        key_prefix: str,
        limits: DeleteLimits,
        delete_missing: bool,
        document_cap: int,
        delete_scope: str = "prefix",
    ) -> None:
        """Missing documents, the delete threshold and the document cap, before any upload.

        Scope `prefix` (the default): the documents whose key starts with the prefix; of those, only
        ones this CLI stamped with the same prefix can be deleted (others are `kept_foreign`).
        Scope `all`: every document of the knowledge base, stamped or not."""
        if delete_scope == "all":
            in_scope = list(existing)
        else:
            in_scope = [k for k in existing if k.startswith(key_prefix)]
        present = scan.on_disk_keys | {f.key for f in scan.files} | {
            document_key(rel, key_prefix) for rel, _ in scan.problems
        }
        selector = scan.selector
        missing: list[str] = []
        for key in in_scope:
            if key in present:
                continue
            # Only a document this run would have produced is a candidate: its key, decoded back to a
            # path, must pass this run's filters (so --include/--exclude/hidden never make a file
            # "missing").
            stripped = key[len(key_prefix):] if key_prefix and key.startswith(key_prefix) else key
            rel = stripped.replace(KEY_SEPARATOR, "/")
            if selector is not None and selector.why_not(rel) is not None:
                if len(key) == MAX_KEY_LENGTH and _SHORTENED_KEY.search(key):
                    # A key shortened with a hash suffix has lost its path: it cannot be checked
                    # against the tree, so it is never deleted, but it is reported.
                    report.kept_unverifiable.append(key)
                continue
            if delete_scope != "all" and not is_ours(existing[key], key_prefix):
                report.kept_foreign.append(key)  # a Studio upload, or another tree's document
                continue
            missing.append(key)
        report.missing = sorted(missing)
        report.kept_unverifiable.sort()
        report.kept_foreign.sort()
        report.in_scope = len(in_scope)
        if delete_missing and (scan.files or scan.problems):  # an empty tree never deletes (see run)
            refusal = limits.refusal(len(report.missing), report.in_scope)
            if refusal is not None:
                report.delete_refused = (
                    f"--delete-missing refused: {refusal}. Check the directory and --key-prefix; raise"
                    " --max-delete and/or --max-delete-percent (or pass --yes-delete-all) to allow it"
                )
                if not report.dry_run:
                    raise DeleteRefused(report)
        new_keys = sum(1 for f in scan.files if f.key not in existing)
        peak = len(existing) + new_keys
        if peak > document_cap:
            problem = (
                f"the knowledge base would pass its {document_cap:,}-document cap ({len(existing)} existing +"
                f" {new_keys} new = {peak}); deletions run only after the uploads, so remove documents"
                " first or import into another knowledge base"
            )
            if report.dry_run:
                report.notes.append(problem)
            else:
                raise UsageError(problem)

    def _delete(self, report: Report) -> None:
        def one(key: str) -> tuple[str, str | None]:
            try:
                response = self.call("DELETE", _doc_path(self.kb, key))
            except TransportError as exc:
                return key, str(exc)[:MAX_REASON_CHARS]
            if response.status in (204, 404):  # 404: already gone
                return key, None
            return key, redact(_reason(response), self._token)

        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="kb-delete") as pool:
            for key, problem in pool.map(one, report.missing):
                if problem is None:
                    report.deleted.append(key)
                else:
                    report.delete_failed.append((key, problem))


def _dry_result(existing: dict[str, dict[str, Any]] | None, f: SourceFile, loaded: LoadedFile) -> str:
    """The dry run's estimate: `would_create`, `would_update` or `would_skip` (unchanged), or
    `would_upload` when the knowledge base could not be read (offline)."""
    if existing is None:
        return "would_upload"
    doc = existing.get(f.key)
    if doc is None:
        return "would_create"
    body = loaded.body
    same = (
        doc.get("content_hash") == loaded.content_hash
        and doc.get("mime_type") == body["mime_type"]
        and (doc.get("title") or "") == body["title"].strip()
        and doc.get("source_url") == body.get("source_url")
        and (doc.get("metadata") or {}) == (body.get("metadata") or {})
    )
    return "would_skip" if same else "would_update"


# ----------------------------------------------------------------------------------------- output

RESULT_LABELS = {
    "created": "created",
    "new_version": "new version",
    "unchanged": "unchanged",
    "retried": "re-ingest started",
    "failed": "failed",
    "would_create": "would create",
    "would_update": "would start a new version",
    "would_skip": "would be unchanged",
    "would_upload": "would upload",
}


def format_report(report: Report, *, delete_missing: bool) -> str:
    lines = [f"knowledge base {report.kb}{' (dry run: nothing uploaded or deleted)' if report.dry_run else ''}"]
    if report.formats:
        lines.append("files by format: " + ", ".join(f"{k} {v}" for k, v in sorted(report.formats.items())))
    if report.skipped:
        lines.append("skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(report.skipped.items())))
    counts = report.counts()
    order = [k for k in RESULT_LABELS if counts.get(k)]
    lines.append("documents: " + (", ".join(f"{RESULT_LABELS[k]} {counts[k]}" for k in order) or "none"))
    if report.unchanged_failed:
        lines.append(
            f"unchanged but their last ingest failed: {len(report.unchanged_failed)} (pass --retry-failed to re-ingest)"
        )
    for o in report.failed:
        lines.append(f"  FAILED {o.rel_path}: {o.reason}")
    if report.missing:
        n = len(report.missing)
        if report.delete_refused is not None:
            lines.append(f"missing from the tree: {n} (NOT deleted: refused, see below)")
        elif report.deletes_skipped is not None:
            lines.append(f"missing from the tree: {n} (NOT deleted: {report.deletes_skipped})")
        elif delete_missing and not report.dry_run:
            lines.append(f"missing from the tree: {n}; deleted {len(report.deleted)}")
        elif delete_missing:
            lines.append(f"missing from the tree: {n} (would be deleted)")
        else:
            lines.append(f"missing from the tree: {n} (kept; pass --delete-missing to delete)")
        verb = "would delete" if delete_missing and report.dry_run else "missing"
        for key in report.missing:  # every key: the dry run lists exactly what would be deleted
            lines.append(f"  {verb} {key}")
    if report.kept_foreign:
        lines.append(
            f"kept, not created by kb_import under this prefix: {len(report.kept_foreign)} (only --delete-scope all deletes them)"
        )
        lines.extend(f"  kept {key}" for key in report.kept_foreign)
    if report.kept_unverifiable:
        lines.append(
            f"kept, shortened keys that cannot be checked against the tree: {len(report.kept_unverifiable)}"
            " (delete them by hand if their file is gone)"
        )
        lines.extend(f"  kept {key}" for key in report.kept_unverifiable)
    for rel_dir, reason in report.walk_errors:
        lines.append(f"  UNREADABLE {rel_dir or '.'}: {reason}")
    if report.walk_errors and not report.deletes_skipped:
        lines.append(f"{len(report.walk_errors)} director(ies) could not be read (their files were not imported)")
    if report.delete_refused is not None:
        lines.append(report.delete_refused)
    for key, problem in report.delete_failed:
        lines.append(f"  DELETE FAILED {key}: {problem}")
    lines.extend(f"note: {n}" for n in report.notes)
    return "\n".join(lines)


# -------------------------------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    """argparse that never echoes argument values in its errors: "unrecognized arguments: --token
    SECRET" would print the secret. Unrecognized arguments are named without their values, and the
    message is scrubbed of the environment's token."""

    secret: str | None = None

    def error(self, message: str) -> Any:  # type: ignore[override]
        if message.startswith("unrecognized arguments:"):
            names = sorted({a.split("=", 1)[0] for a in message.split(":", 1)[1].split() if a.startswith("--")})
            message = "unrecognized arguments" + (f" (values not shown): {' '.join(names)}" if names else " (values not shown)")
        # "invalid int value: 'X'" and "invalid choice: 'X' (choose from ...)" quote the value.
        message = re.sub(r"invalid ([\w ]*?)value: .*", r"invalid \1value (value not shown)", message)
        message = re.sub(r"invalid choice: .*?(\(choose from .*\))?$", r"invalid choice (value not shown) \1", message).rstrip()
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {redact(message, self.secret)}\n")


def _token_flag(argv: Sequence[str]) -> bool:
    """Whether argv tries to pass a token (`--token`, `--token=...`, `--tok`, ...): refused before
    argparse, whose errors would print the next argument (the token) as a directory or value."""
    for arg in argv:
        if arg == "--":
            return False
        name = arg.split("=", 1)[0]
        if name.startswith("--tok") and name != "--token-file":
            return True
    return False


def _max_delete(value: str) -> int | None:
    if value.strip().lower() == "all":
        return None
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of documents or 'all'") from None
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return n


def _percent(value: str) -> float:
    try:
        n = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number from 0 to 100") from None
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError("must be a number from 0 to 100")
    return n


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if n < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="kb_import.py",
        allow_abbrev=False,  # `--token X` must be an error, not `--token-file X` (which would echo X)
        description="Import a directory tree into a knowledge base through the API. The token comes from "
        f"{TOKEN_ENV} or --token-file (never the command line).",
    )
    p.add_argument("directory", type=Path, help="the tree to import")
    p.add_argument("--kb", required=True, help="the knowledge base id")
    p.add_argument("--api-url", default=None, help=f"API base URL (default ${API_URL_ENV} or {DEFAULT_API_URL})")
    p.add_argument("--token-file", type=Path, default=None, help=f"read the bearer token from this file instead of ${TOKEN_ENV}")
    p.add_argument("--include", action="append", default=[], metavar="GLOB", help="only files matching a glob (repeatable)")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip files matching a glob (repeatable)")
    p.add_argument("--include-hidden", action="store_true", help="also import dot-files and dot-directories")
    p.add_argument("--key-prefix", default="", help="prefix every document key (and scope --delete-missing to it)")
    p.add_argument(
        "--source-url-template",
        default=None,
        metavar="TEMPLATE",
        help="citation URL per file, e.g. 'https://docs.example.com/{path_no_ext}.html'; placeholders: "
        + ", ".join("{" + f + "}" for f in TEMPLATE_FIELDS),
    )
    p.add_argument("--metadata-map", type=Path, default=None, metavar="MAP.yaml", help="glob -> flat metadata (first match wins)")
    p.add_argument("--concurrency", type=int, default=4, help=f"parallel uploads, 1 to {MAX_CONCURRENCY} (default 4)")
    p.add_argument("--dry-run", action="store_true", help="count files per format and estimate changes; upload nothing")
    p.add_argument("--delete-missing", action="store_true", help="delete knowledge-base documents whose file is gone")
    p.add_argument(
        "--max-delete",
        type=_max_delete,
        default=argparse.SUPPRESS,
        metavar="N|all",
        help=f"allow --delete-missing to delete up to N documents, or any number with 'all' (default {DEFAULT_MAX_DELETE});"
        " the --max-delete-percent rule still applies",
    )
    p.add_argument(
        "--max-delete-percent",
        type=_percent,
        default=DEFAULT_MAX_DELETE_PERCENT,
        metavar="P",
        help=f"refuse a deletion over P %% of the documents in the delete scope (default {DEFAULT_MAX_DELETE_PERCENT:g}; 100 lifts it)",
    )
    p.add_argument("--yes-delete-all", action="store_true", help="lift both --delete-missing limits (count and percent)")
    p.add_argument(
        "--delete-scope",
        choices=("prefix", "all"),
        default="prefix",
        help="what --delete-missing may delete: 'prefix' (default; needs --key-prefix) deletes only documents"
        " this CLI created under that prefix; 'all' makes every document of the knowledge base a candidate",
    )
    p.add_argument(
        "--max-file-bytes",
        type=_positive_int,
        default=DEFAULT_MAX_FILE_BYTES,
        metavar="N",
        help=f"fail files over N bytes without reading them (default {DEFAULT_MAX_FILE_BYTES})",
    )
    p.add_argument("--retry-failed", action="store_true", help="re-ingest unchanged documents whose last ingest failed")
    p.add_argument("--report-json", type=Path, default=None, metavar="PATH", help="also write the report as JSON")
    p.add_argument("--timeout", type=float, default=300.0, help="per-request timeout in seconds (default 300)")
    return p


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[str, str], ApiClient] | None = None,
    environ: dict[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    out: Any = None,
    err: Any = None,
) -> int:
    """The CLI. Tests pass `client_factory(api_url, token) -> ApiClient`, `environ`, `sleep` and
    the output streams; the default factory builds an HttpxClient."""
    out = out or sys.stdout
    err = err or sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ) if environ is None else environ
    if _token_flag(argv):
        print(
            f"error: the token is never taken from the command line (value not shown); set {TOKEN_ENV} or pass"
            " --token-file",
            file=err,
        )
        return EXIT_USAGE
    parser = build_parser()
    parser.secret = (env.get(TOKEN_ENV) or "").strip() or None  # type: ignore[attr-defined]
    args = parser.parse_args(argv)
    token: str | None = None
    client: ApiClient | None = None
    try:
        root: Path = args.directory
        if not root.is_dir():
            raise UsageError(f"{root} is not a directory")
        if args.source_url_template:
            check_template(args.source_url_template)
        api_url = args.api_url or env.get(API_URL_ENV) or DEFAULT_API_URL
        check_api_url(api_url)
        if args.key_prefix and key_problem(args.key_prefix + "x") is not None:
            raise UsageError("--key-prefix must not contain '\\', control characters or surrounding whitespace")
        if "/" in args.key_prefix:
            raise UsageError("--key-prefix must not contain '/'")
        if args.report_json is not None:
            check_report_path(args.report_json)
        metadata_map = load_metadata_map(args.metadata_map) if args.metadata_map else None
        token = read_token(args.token_file, env)
        if token is None and not args.dry_run:
            raise UsageError(f"no API token: set {TOKEN_ENV} or pass --token-file")
        key_prefix = normalise_prefix(args.key_prefix)
        if key_prefix != args.key_prefix:
            print(f"note: --key-prefix {args.key_prefix!r} is used as {key_prefix!r} (a whole key segment)", file=err)
        if args.yes_delete_all:
            limits = DeleteLimits(None, None)
        else:
            # Independent rules: --max-delete N|all sets the count only, the percent rule stays.
            limits = DeleteLimits(getattr(args, "max_delete", DEFAULT_MAX_DELETE), args.max_delete_percent)
        scan = scan_tree(
            root,
            include=Globs(args.include),
            exclude=Globs(args.exclude),
            key_prefix=key_prefix,
            include_hidden=args.include_hidden,
            max_file_bytes=args.max_file_bytes,
        )
        if token is not None:
            factory = client_factory or (lambda url, tok: HttpxClient(url, tok, timeout=args.timeout))
            client = factory(api_url, token)
        importer = Importer(
            client if client is not None else _NoClient(),
            args.kb,
            concurrency=args.concurrency,
            sleep=sleep,
            token=token,
        )
        report = importer.run(
            scan,
            metadata_map=metadata_map,
            source_url_template=args.source_url_template,
            dry_run=args.dry_run,
            delete_missing=args.delete_missing,
            retry_failed=args.retry_failed,
            key_prefix=key_prefix,
            online=client is not None,
            delete_limits=limits,
            max_file_bytes=args.max_file_bytes,
            delete_scope=args.delete_scope,
        )
    except DeleteRefused as exc:
        # Nothing was uploaded or deleted: print the plan (every key that would have been deleted).
        print(redact(format_report(exc.report, delete_missing=True), token), file=out)
        print(f"error: {redact(str(exc), token)}", file=err)
        _write_json(args.report_json, exc.report, token, err)
        return EXIT_USAGE
    except UsageError as exc:
        print(f"error: {redact(str(exc), token)}", file=err)
        return EXIT_USAGE
    except TransportError as exc:
        print(f"error: the API is unreachable: {redact(str(exc), token)}", file=err)
        return EXIT_USAGE
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    print(redact(format_report(report, delete_missing=args.delete_missing), token), file=out)
    written = _write_json(args.report_json, report, token, err)
    if report.delete_refused is not None:
        return EXIT_USAGE  # a dry run over the delete limits exits like the real run would
    if report.failed or report.delete_failed or report.deletes_skipped or report.walk_errors or not written:
        return EXIT_FAILURES
    return EXIT_OK


def check_report_path(path: Path) -> None:
    """--report-json is checked before anything is uploaded: its directory must exist (it is never
    created), be a directory and be writable, and the path must not be a directory nor an existing
    file that cannot be written, so the report of a finished run is never lost to a bad path. Each
    is a usage error (exit 2)."""
    if path.is_dir():
        raise UsageError(f"--report-json {path} is a directory")
    parent = path.parent
    if not parent.exists():
        raise UsageError(f"--report-json: the directory {parent} does not exist")
    if not parent.is_dir():
        raise UsageError(f"--report-json: {parent} is not a directory")
    if not os.access(parent, os.W_OK | os.X_OK):
        raise UsageError(f"--report-json: the directory {parent} is not writable")
    if path.exists() and not os.access(path, os.W_OK):
        raise UsageError(f"--report-json: {path} exists and is not writable")


def _write_json(path: Path | None, report: Report, token: str | None, err: Any = None) -> bool:
    """Write the JSON report; False (with a message on `err`, never a traceback) when it cannot be
    written (the directory went away or is not writable after the up-front check)."""
    if not path:
        return True
    try:
        path.write_text(redact(json.dumps(report.to_json(), indent=2), token) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"error: the JSON report could not be written to {path}: {exc.strerror or type(exc).__name__}",
              file=err or sys.stderr)
        return False
    return True


class _NoClient:
    def request(self, method: str, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None) -> ApiResponse:
        raise AssertionError("no API client in an offline dry run")  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
