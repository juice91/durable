"""`GithubRepositoryDocsAdapter`: files of a GitHub repository as knowledge-base documents.

Source config (validated by `validate_config`; the connection's config merges under it):

| key | meaning |
|---|---|
| `repository` | `owner/name` (required) |
| `ref` | branch, tag or commit (default: the repository's default branch) |
| `include` | globs over repository paths (>= 1, required); `exclude`: globs (optional) |
| `map` | ordered rules `[{match: <glob>, metadata: {...}}]`, first match wins (see mapping.py) |
| `max_bytes` | per-file cap (default 5 MiB for text, 25 MiB for PDF; never above those) |
| `document_policy` | `{types: [md, rst, html, txt, yaml, yml, json, pdf], delete_missing: bool}` |
| `key_prefix` | document-key prefix (default `source:<source_id>:`); the key is the prefix + the path with `/` as `~` |

Connection (kind `api_token`): optional secret ref `token` (a public repository works without one under
GitHub's unauthenticated limit of 60 requests an hour); `base_url` defaults to `https://api.github.com`.

Modes. `targeted`: the given paths (each resolved with one contents call at the ref's head commit).
`incremental`: the head commit and the source config unchanged since the last complete walk -> nothing to do
(`unchanged` = the files seen then); otherwise a walk. `full`: a walk. A walk lists the head commit's tree
(`git/trees?recursive=1`; a truncated answer falls back to per-directory listing, pruned by the include
prefixes and bounded), keeps the files that pass include/exclude/types, and processes them in order in
segments of at most 200 files / `call_budget` fetches. A file whose blob sha (and mapped metadata) is
the ledger's costs no call; a new or changed file is fetched (contents call, or the raw URL above 1 MiB), checked
against its blob sha and handed to `eap.kb_ingest` with the mapped metadata. Ledger: `source_revision` =
`<blob sha>:<metadata hash>`.

Cursor of an unfinished walk: `{commit, tree, offset, failed, rate?}`; of a complete one `{commit, cfg, files}`
(or `{files}` when the walk had failures, so the next incremental sync walks again). `rate` =
`{remaining, reset}` is kept when the rate limit stopped the segment: the next segment makes no call before
the reset time and returns at once (the worker then stops the run with reason `budget`).

At the end of a complete walk the ledger rows whose files are gone (deleted, excluded or renamed) become `removed`;
with `document_policy.delete_missing` their knowledge-base documents are deleted too, guarded like the CLI
(at most 50 documents and 20 % of the ledger), else an error entry says it was refused.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import posixpath
import random
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from importlib import metadata as importlib_metadata
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2

from eap_kb_sources.adapter import (
    ConnectionCheck,
    ConnectionSnapshot,
    EntityRef,
    SecretResolver,
    SegmentCounters,
    SourceStoreFacade,
    SyncSegmentRequest,
    SyncSegmentResult,
)
from eap_kb_sources.errors import (
    INVALID_DOCUMENT,
    INVALID_REQUEST,
    KB_CAPACITY,
    NEEDS_ATTENTION_CODES,
    NOT_FOUND,
    PROVIDER_ERROR,
    RATE_LIMITED,
    RESPONSE_TOO_LARGE,
    SECRET_MISSING,
    CONNECTION_RESPONSE_INVALID,
    SourceError,
    error_entry,
)
from eap_kb_sources.providers.github.client import (
    API_HOST,
    RAW_HOST,
    GithubHttp,
    has_control_chars,
)
from eap_kb_sources.providers.github.mapping import (
    Globs,
    MetadataMap,
    dir_may_match,
    parse_globs,
    parse_map,
)

DEFAULT_API_URL = "https://api.github.com"
SEGMENT_FILES = 200
MAX_TREE_CALLS = 400  # per-directory fallback listing of a truncated tree
INLINE_BYTES = 1024 * 1024  # the contents API returns a file's content inline up to 1 MiB
TEXT_CAP = 5 * 1024 * 1024  # eap_api.schemas / EAP_KB_MAX_DOCUMENT_BYTES default
PDF_CAP = 25 * 1024 * 1024
MAX_PATH_CHARS = 1024
MAX_KEY_CHARS = 256
RELATIONSHIP = "file"
ENTITY_TYPE = "repository"
DELETE_MAX_COUNT = 50
DELETE_MAX_PERCENT = 20.0
EXTENSION_MIME = {
    "md": "text/markdown", "markdown": "text/markdown", "rst": "text/x-rst", "html": "text/html",
    "htm": "text/html", "txt": "text/plain", "yaml": "application/yaml", "yml": "application/yaml",
    "json": "application/json", "pdf": "application/pdf",
}
DEFAULT_TYPES = ("md", "rst", "html", "txt", "yaml", "yml", "json", "pdf")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_PATH_TARGET = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")


def _user_agent() -> str:
    try:
        version = importlib_metadata.version("eap-kb-sources")
    except importlib_metadata.PackageNotFoundError:
        version = "0"
    return f"PydanticOS-source-sync/{version}"


def _is_int(value: Any, *, minimum: int, maximum: int | None = None) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum and (maximum is None or value <= maximum)


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def path_problem(path: Any) -> str | None:
    """Why a repository path cannot be a document path (traversal, absolute, control characters), or None."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
        return "the path is empty or too long"
    if has_control_chars(path) or "\\" in path:
        return "the path has a control character or a backslash"
    if path.startswith("/") or path.endswith("/"):
        return "the path is absolute or names a directory"
    if any(part in ("", ".", "..") for part in path.split("/")):
        return "the path is not normalised (traversal refused)"
    return None


def extension(path: str) -> str:
    return posixpath.splitext(path)[1][1:].lower()


class _Config:
    """The source's config, parsed. `validate` is the ValueError path of `validate_config`."""

    def __init__(self, raw: Mapping[str, Any], source_id: str = "") -> None:
        self.raw = raw
        repository = raw.get("repository")
        if not isinstance(repository, str) or not _REPOSITORY.match(repository) or repository.endswith(".git"):
            raise ValueError("repository must be 'owner/name'")
        self.repository = repository
        ref = raw.get("ref")
        if ref is not None and (
            not isinstance(ref, str) or not ref.strip() or len(ref) > 200 or has_control_chars(ref) or " " in ref or ".." in ref
        ):
            raise ValueError("ref must be a branch, tag or commit name")
        self.ref = ref or "HEAD"
        self.include = parse_globs("include", raw.get("include"), required=True)
        self.exclude = parse_globs("exclude", raw.get("exclude"), required=False)
        self.map: MetadataMap = parse_map(raw.get("map"))
        policy = raw.get("document_policy")
        if policy is not None and not isinstance(policy, Mapping):
            raise ValueError("document_policy must be an object")
        policy = policy or {}
        types = policy.get("types", list(DEFAULT_TYPES))
        if not isinstance(types, list) or not types or not all(isinstance(t, str) and t in EXTENSION_MIME for t in types):
            raise ValueError(f"document_policy.types must be a non-empty list of {', '.join(DEFAULT_TYPES)}")
        self.types = frozenset(types)
        delete_missing = policy.get("delete_missing", False)
        if not isinstance(delete_missing, bool):
            raise ValueError("document_policy.delete_missing must be true or false")
        self.delete_missing = delete_missing
        max_bytes = raw.get("max_bytes", policy.get("max_bytes"))
        if max_bytes is not None and not _is_int(max_bytes, minimum=1, maximum=PDF_CAP):
            raise ValueError(f"max_bytes must be an integer from 1 to {PDF_CAP}")
        self.max_bytes = max_bytes
        prefix = raw.get("key_prefix", f"source:{source_id}:" if source_id else "")
        if not isinstance(prefix, str) or len(prefix) > 120 or has_control_chars(prefix) or "\\" in prefix or prefix != prefix.strip():
            raise ValueError("key_prefix must be a short printable string without backslash")
        self.key_prefix = prefix

    def cap(self, ext: str) -> int:
        base = PDF_CAP if ext == "pdf" else TEXT_CAP
        return min(base, self.max_bytes) if self.max_bytes else base

    def admits(self, path: str) -> bool:
        return self.include.matches(path) and not self.exclude.matches(path) and extension(path) in self.types

    def fingerprint(self) -> str:
        body = json.dumps(
            {
                "repository": self.repository, "ref": self.ref, "include": self.include.patterns,
                "exclude": self.exclude.patterns, "map": [[r.glob, r.metadata] for r in self.map.rules],
                "types": sorted(self.types), "max_bytes": self.max_bytes, "key_prefix": self.key_prefix,
            },
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    def key(self, path: str) -> str:
        key = self.key_prefix + unicodedata.normalize("NFC", path).replace("/", "~")
        if len(key) > MAX_KEY_CHARS:
            digest = hashlib.sha256(key.encode()).hexdigest()[:12]
            key = key[: MAX_KEY_CHARS - 13] + "~" + digest
        return key

    def metadata(self, path: str) -> dict[str, Any]:
        return {**self.map.lookup(path), "path": path[:256]}


class _Tree:
    def __init__(self) -> None:
        self.files: list[dict[str, Any]] = []  # {path, sha, size, mode}
        self.complete = True


class GithubRepositoryDocsAdapter:
    kind = "repository_docs"
    provider = "github"
    # Optional display data for Studio's forms (GET /kb-sources/adapters).
    display_name = "GitHub"
    connection_kind = "api_token"
    default_base_url = "https://api.github.com"

    def __init__(
        self,
        *,
        http_factory: Callable[[], httpx2.AsyncClient] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._http_factory = http_factory
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter or (lambda: random.uniform(0.0, 0.5))

    def _new_http(self) -> httpx2.AsyncClient:
        if self._http_factory is not None:
            return self._http_factory()
        from eap_secrets.outbound import OutboundPolicy

        return OutboundPolicy.from_env().http_client()

    # ---------------------------------------------------------------------------------- config

    def validate_config(self, config: Mapping[str, Any]) -> None:
        hosts = config.get("allowed_hosts")
        if hosts is not None and (
            not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) and _HOST.match(h) for h in hosts)
        ):
            raise ValueError("allowed_hosts must be a non-empty list of host names")
        _Config(config)

    @staticmethod
    def _base(connection: ConnectionSnapshot) -> tuple[str, frozenset[str]]:
        base = (connection.base_url or DEFAULT_API_URL).rstrip("/")
        host = (urlsplit(base).hostname or API_HOST).lower()
        extra = connection.config.get("allowed_hosts")
        hosts = {host, RAW_HOST, *(h.lower() for h in extra if isinstance(h, str))} if isinstance(extra, list) else {host, RAW_HOST}
        return base, frozenset(hosts)

    @staticmethod
    def _token(connection: ConnectionSnapshot, secrets: SecretResolver) -> str | None:
        ref = connection.secret_refs.get("token")
        if not ref:
            return None  # a public repository without a token
        value = secrets.resolve(ref)
        if not value:
            raise SourceError(SECRET_MISSING, "the token secret is empty")
        return value

    # ---------------------------------------------------------------------------------- check

    async def check_connection(self, connection: ConnectionSnapshot, secrets: SecretResolver) -> ConnectionCheck:
        try:
            token = self._token(connection, secrets)
            base, hosts = self._base(connection)
            async with self._new_http() as client:
                http = GithubHttp(client, hosts, user_agent=_user_agent(), token=token, sleep=self._sleep, jitter=self._jitter)
                await http.get_json(f"{base}/rate_limit")  # free; 401 for a bad token
        except SourceError as exc:
            return ConnectionCheck(False, exc.code)
        return ConnectionCheck(True)

    # ---------------------------------------------------------------------------------- segment

    async def sync_segment(
        self,
        request: SyncSegmentRequest,
        connection: ConnectionSnapshot,
        secrets: SecretResolver,
        store: SourceStoreFacade,
    ) -> SyncSegmentResult:
        try:
            cfg = _Config(request.config, request.source_id)
        except ValueError as exc:
            raise SourceError(INVALID_REQUEST, f"invalid configuration: {str(exc)[:150]}") from None
        token = self._token(connection, secrets)  # a missing secret fails before any network call
        base, hosts = self._base(connection)
        cursor = dict(request.cursor)
        rate = cursor.get("rate")
        if (
            isinstance(rate, Mapping) and request.mode != "targeted"
            and rate.get("remaining", 1) <= 0 and self._clock() < float(rate.get("reset", 0))
        ):  # the limit is still spent: no call, the worker stops the run
            return SyncSegmentResult(cursor, False, SegmentCounters(), [], 0)
        counters, errors = SegmentCounters(), []
        async with self._new_http() as client:
            http = GithubHttp(client, hosts, user_agent=_user_agent(), token=token, sleep=self._sleep, jitter=self._jitter)
            walk = _Walk(self, http, base, cfg, request, store, counters, errors)
            try:
                done, next_cursor, revision = await walk.run()
            except SourceError as exc:
                if exc.code != RATE_LIMITED:
                    raise
                walk.rate_stopped = True
                done, next_cursor, revision = False, walk.resume_cursor(), None
        if walk.rate_stopped:
            reset = http.rate_reset if http.rate_reset is not None else int(self._clock()) + 3600
            next_cursor = {**(next_cursor or {}), "rate": {"remaining": 0, "reset": reset}}
        elif next_cursor is not None:
            next_cursor.pop("rate", None)
        return SyncSegmentResult(
            next_cursor, done, counters, errors[:50], http.calls, sweep_ok=walk.sweep_ok, source_revision=revision,
            budget={"used": {"requests": http.calls}, "remaining": {"requests": max(0, http.rate_remaining)}}
            if http.rate_remaining is not None else None,
        )


class _Walk:
    def __init__(self, adapter, http, base, cfg, request, store, counters, errors) -> None:
        self.adapter, self.http, self.base, self.cfg = adapter, http, base, cfg
        self.request, self.store, self.counters, self.errors = request, store, counters, errors
        self.rate_stopped = False
        self.sweep_ok = True
        self.entity: EntityRef | None = None
        self.head = ""
        self.tree_sha = ""
        self.offset = 0
        self.failed_total = int(request.cursor.get("failed", 0) or 0)

    # ------------------------------------------------------------------------------- helpers

    def repo_url(self, tail: str) -> str:
        return f"{self.base}/repos/{self.cfg.repository}/{tail}"

    def blob_url(self, path: str) -> str:
        return f"https://github.com/{self.cfg.repository}/blob/{self.head}/{quote(path, safe='/')}"

    def resume_cursor(self) -> dict[str, Any]:
        out = {k: v for k, v in self.request.cursor.items() if k in ("commit", "tree", "offset", "failed", "cfg", "files")}
        if self.tree_sha:
            out.update({"commit": self.head, "tree": self.tree_sha, "offset": self.offset, "failed": self.failed_total})
        return out

    def fail(self, external_id: str, exc: BaseException) -> None:
        self.counters.failed += 1
        self.failed_total += 1
        self.errors.append(error_entry(external_id, exc))

    async def resolve_head(self) -> None:
        commit = await self.http.get_json(self.repo_url(f"commits/{quote(self.cfg.ref, safe='')}"))
        sha = commit.get("sha") if isinstance(commit, dict) else None
        tree = ((commit.get("commit") or {}).get("tree") or {}).get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(r"[0-9a-f]{40}", tree):
            raise SourceError(CONNECTION_RESPONSE_INVALID, "the commit response is not valid")
        self.head, self.tree_sha = sha, tree

    def upsert_entity(self, files: int | None = None) -> EntityRef:
        attributes: dict[str, Any] = {"ref": self.cfg.ref, "commit": self.head}
        if files is not None:
            attributes["files"] = files
        self.entity = self.store.upsert_entity(
            self.cfg.repository, entity_type=ENTITY_TYPE, attributes=attributes,
            identifiers=[("repository", self.cfg.repository, True), ("ref", self.cfg.ref, False), ("commit", self.head, False)],
        )
        return self.entity

    # --------------------------------------------------------------------------------- entry

    async def run(self) -> tuple[bool, dict[str, Any], str | None]:
        if self.request.mode == "targeted":
            return await self.targeted()
        return await self.walk()

    # ------------------------------------------------------------------------------- targeted

    async def targeted(self) -> tuple[bool, dict[str, Any], str | None]:
        await self.resolve_head()
        self.upsert_entity()
        for path in self.request.part_numbers:
            problem = path_problem(path)
            if problem is None and not _PATH_TARGET.match(path):
                problem = "the path has characters a targeted sync does not take"
            if problem is None and not self.cfg.admits(path):
                problem = "the path is not in the source's include/exclude/types"
            if problem is not None:
                self.fail(path, SourceError(INVALID_REQUEST, problem))
                continue
            await self.file(path, sha=None, size=None, mode="100644")
        cursor = {k: v for k, v in self.request.cursor.items() if k == "daily_budget"}
        return True, cursor, None

    # ------------------------------------------------------------------------------------ walk

    async def walk(self) -> tuple[bool, dict[str, Any], str | None]:
        cursor, cfg = self.request.cursor, self.cfg
        resuming = all(k in cursor for k in ("commit", "tree", "offset"))
        if resuming:
            self.head, self.tree_sha, self.offset = str(cursor["commit"]), str(cursor["tree"]), max(0, int(cursor["offset"]))
        else:
            await self.resolve_head()
            if (
                self.request.mode == "incremental" and cursor.get("commit") == self.head
                and cursor.get("cfg") == cfg.fingerprint()
            ):
                files = int(cursor.get("files", 0) or 0)
                self.counters.discovered += files
                self.counters.unchanged += files
                return True, {"commit": self.head, "cfg": cfg.fingerprint(), "files": files}, self.head
        tree = await self.list_tree()
        files = tree.files
        if not tree.complete:
            self.sweep_ok = False
            self.errors.append(error_entry(None, SourceError(PROVIDER_ERROR, "the tree listing was incomplete")))
        entity = self.upsert_entity(len(files))
        listing_hash = hashlib.sha256("".join(f"{f['path']}\0{f['sha']}\n" for f in files).encode()).hexdigest()
        self.store.upsert_record(cfg.repository, "tree", {"commit": self.head, "files": len(files), "listing_hash": listing_hash})
        fetched, processed = 0, 0
        while self.offset < len(files) and processed < SEGMENT_FILES:
            f = files[self.offset]
            if self.http.rate_remaining is not None and self.http.rate_remaining <= 0:
                self.rate_stopped = True
                break
            if f["mode"] == "120000":
                self.fail(f["path"], SourceError(INVALID_DOCUMENT, "a symlink is not a document (refused)"))
            else:
                before = self.http.calls
                try:
                    await self.file(f["path"], sha=f["sha"], size=f["size"], mode=f["mode"])
                except _Capacity:
                    self.offset = len(files)
                    break
                fetched += self.http.calls - before
            self.offset += 1
            processed += 1
            if fetched >= self.request.call_budget:
                break
        if self.rate_stopped or self.offset < len(files):
            return False, self.resume_cursor(), None
        self.finish_ledger(entity, files)
        if self.failed_total:
            return True, {"files": len(files), "cfg": cfg.fingerprint()}, self.head
        return True, {"commit": self.head, "cfg": cfg.fingerprint(), "files": len(files)}, self.head

    async def list_tree(self) -> _Tree:
        tree = _Tree()
        data = await self.http.get_json(self.repo_url(f"git/trees/{self.tree_sha}?recursive=1"))
        entries = data.get("tree") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise SourceError(CONNECTION_RESPONSE_INVALID, "the tree response is not valid")
        if data.get("truncated"):
            await self.list_directories(tree)
        else:
            self.collect(tree, entries, "")
        tree.files.sort(key=lambda f: f["path"])
        return tree

    def collect(self, tree: _Tree, entries: list[Any], prefix: str) -> None:
        for e in entries:
            if not isinstance(e, dict) or e.get("type") != "blob" or not isinstance(e.get("path"), str):
                continue  # trees and submodules (type "commit") are not documents
            path = prefix + e["path"]
            if path_problem(path) is not None or not self.cfg.admits(path):
                continue
            sha, size, mode = e.get("sha"), e.get("size"), str(e.get("mode", "100644"))
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                continue
            tree.files.append({"path": path, "sha": sha, "size": size if isinstance(size, int) else None, "mode": mode})

    async def list_directories(self, tree: _Tree) -> None:
        """The truncated fallback: list the root, then each directory that can hold an included file."""
        prefixes = self.cfg.include.prefixes()
        queue: list[tuple[str, str]] = [("", self.tree_sha)]
        calls = 0
        while queue:
            prefix, sha = queue.pop(0)
            if calls >= MAX_TREE_CALLS:
                tree.complete = False
                return
            calls += 1
            data = await self.http.get_json(self.repo_url(f"git/trees/{sha}"))
            entries = data.get("tree") if isinstance(data, dict) else None
            if not isinstance(entries, list) or data.get("truncated"):
                tree.complete = False
                continue
            self.collect(tree, entries, prefix)
            for e in entries:
                if isinstance(e, dict) and e.get("type") == "tree" and isinstance(e.get("path"), str) and isinstance(e.get("sha"), str):
                    directory = prefix + e["path"]
                    if path_problem(directory) is None and dir_may_match(directory, prefixes):
                        queue.append((directory + "/", e["sha"]))

    # ------------------------------------------------------------------------------- one file

    async def file(self, path: str, *, sha: str | None, size: int | None, mode: str) -> None:
        """Fetch (when changed), verify and hand one file off. Counts its own failures; raises RATE_LIMITED,
        the attention codes and `_Capacity` only."""
        cfg, store, counters = self.cfg, self.store, self.counters
        entity = self.entity
        ext = extension(path)
        metadata = cfg.metadata(path)
        meta_hash = hashlib.sha1(json.dumps(metadata, sort_keys=True).encode(), usedforsecurity=False).hexdigest()[:8]
        key = cfg.key(path)
        try:
            if size is not None and size > cfg.cap(ext):
                raise SourceError(RESPONSE_TOO_LARGE, f"the file is over the {cfg.cap(ext)} byte cap")
            data: bytes | None = None
            if sha is None:  # targeted: the contents call names the blob
                data, sha, size = await self.contents(path, size)
            counters.discovered += 1
            revision = f"{sha}:{meta_hash}"
            url = self.blob_url(path)
            state = store.document_state(entity, RELATIONSHIP, url, key=key)
            if state is not None and state.source_revision == revision:
                counters.unchanged += 1
                return
            if state is None:
                counters.documents_discovered += 1
            if data is None:
                data, _, _ = await self.contents(path, size, expected=sha)
            if len(data) > cfg.cap(ext):
                raise SourceError(RESPONSE_TOO_LARGE, f"the file is over the {cfg.cap(ext)} byte cap")
            if git_blob_sha(data) != sha:
                raise SourceError(PROVIDER_ERROR, "the content does not match its blob sha")
            outcome = store.submit_document(
                entity, RELATIONSHIP, url, data, mime_type=EXTENSION_MIME[ext], title=path[:200],
                source_revision=revision, metadata=metadata, key=key,
            )
            if outcome == "queued":
                counters.documents_queued += 1
                if state is None:
                    counters.inserted += 1
                else:
                    counters.updated += 1
            else:
                counters.unchanged += 1
        except SourceError as exc:
            if exc.code in NEEDS_ATTENTION_CODES or exc.code == RATE_LIMITED:
                raise
            if exc.code == KB_CAPACITY:
                self.fail(path, exc)
                raise _Capacity from None
            self.fail(path, exc)

    async def contents(self, path: str, size: int | None, expected: str | None = None) -> tuple[bytes, str, int]:
        """(bytes, blob sha, size). Up to 1 MiB: one contents call (base64); above it the raw URL at the head
        commit (a contents call first when the sha is not known yet)."""
        quoted = quote(path, safe="/")
        if size is not None and size > INLINE_BYTES:
            raw = await self.http.get(
                f"https://{RAW_HOST}/{self.cfg.repository}/{self.head}/{quoted}", accept="application/octet-stream",
                max_bytes=self.cfg.cap(extension(path)),
            )
            return raw.body, expected or "", len(raw.body)
        meta = await self.http.get_json(self.repo_url(f"contents/{quoted}?ref={self.head}"))
        if not isinstance(meta, dict) or isinstance(meta, list):
            raise SourceError(INVALID_DOCUMENT, "the path is a directory")
        kind = meta.get("type")
        if kind != "file":
            raise SourceError(INVALID_DOCUMENT, f"a {str(kind)[:20]} is not a document (refused)")
        sha, real_size = meta.get("sha"), meta.get("size")
        if not isinstance(sha, str) or not isinstance(real_size, int):
            raise SourceError(CONNECTION_RESPONSE_INVALID, "the contents response is not valid")
        if real_size > self.cfg.cap(extension(path)):
            raise SourceError(RESPONSE_TOO_LARGE, "the file is over the byte cap")
        if real_size > INLINE_BYTES:
            if expected is None:
                return b"", sha, real_size  # the caller compares the ledger before the raw download
            return await self.contents(path, real_size, expected=sha)
        try:
            content = base64.b64decode(str(meta.get("content", "")), validate=False)
        except (binascii.Error, ValueError):
            raise SourceError(CONNECTION_RESPONSE_INVALID, "the content is not base64") from None
        return content, sha, real_size

    # ------------------------------------------------------------------------------- the ledger

    def finish_ledger(self, entity: EntityRef, files: list[dict[str, Any]]) -> None:
        """A complete walk: ledger rows of files that are gone become `removed`; with delete_missing their
        knowledge-base documents go too, within the CLI's guard. Only keys under this source's prefix."""
        if not self.sweep_ok:
            return
        cfg = self.cfg
        present = {cfg.key(f["path"]) for f in files}
        ledger = [d for d in self.store.list_documents(entity, RELATIONSHIP) if d.key.startswith(cfg.key_prefix)]
        gone = [d for d in ledger if d.key not in present]
        if not gone:
            return
        delete = cfg.delete_missing
        if delete and (len(gone) > DELETE_MAX_COUNT or len(gone) * 100 > DELETE_MAX_PERCENT * len(ledger)):
            delete = False
            self.errors.append(error_entry(None, SourceError(
                PROVIDER_ERROR, f"delete_missing refused: {len(gone)} of {len(ledger)} documents (limits 50 and 20 %)"
            )))
        for d in gone:
            self.store.remove_document(entity, d.key, delete=delete)


class _Capacity(Exception):
    """The knowledge base is full: the rest of the walk would fail the same way."""
