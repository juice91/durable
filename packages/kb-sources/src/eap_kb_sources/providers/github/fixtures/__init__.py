"""A fake GitHub API for tests (never the real one), and the repositories it serves.

`FakeGithub` is an `httpx2.MockTransport` handler: `httpx2.AsyncClient(transport=httpx2.MockTransport(fake))`.
It serves, for each repository it holds (`owner/name`, files and symlinks at a head commit):

| route | answer |
|---|---|
| `GET /rate_limit` | the token's (or the anonymous) limit |
| `GET /repos/{r}/commits/{ref}` | `{sha, commit: {tree: {sha}}}` for a branch, `HEAD` or a commit sha |
| `GET /repos/{r}/git/trees/{sha}[?recursive=1]` | tree entries with blob shas, sizes and modes; a recursive answer longer than `tree_limit` is cut and `truncated: true` |
| `GET /repos/{r}/contents/{path}?ref=` | `{type, sha, size, encoding, content}`; a file above 1 MiB has empty content, a symlink has type `symlink` |
| `GET raw.githubusercontent.com/{r}/{commit}/{path}` | the file's bytes |

Shas are real git shas (blob = sha1 of `blob <n>\\0<bytes>`, trees hashed from their entries), so an
adapter's integrity check works against it. Every response carries `x-ratelimit-*` headers; once the
remaining count is spent (`limit`, default 5000 with a token, 60 without) every call answers 403 with
`x-ratelimit-remaining: 0`; `reset` is the epoch time it returns. `requests` records `(host, path, query,
authorized)` for every call (never the token). `script(path, [responders])` queues responses for one path.
`sdk_like_repo()` builds the acceptance repository: 30 documents (md, rst, yaml, pdf) in nested paths named
after the Acme SDK repositories, one symlink, one oversize file.
"""

from __future__ import annotations

import hashlib
import json
from base64 import encodebytes
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx2

TOKEN = "GITHUB-TOKEN-SENTINEL-4c1e"
API_HOST = "api.github.com"
RAW_HOST = "raw.githubusercontent.com"
INLINE = 1024 * 1024

Responder = Callable[[httpx2.Request], httpx2.Response]


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def pdf_bytes(label: str, revision: int = 1, pad: int = 0) -> bytes:
    return (f"%PDF-1.4\n% {label} revision {revision}\n" + "x" * pad + "\n%%EOF\n").encode("ascii")


def status(code: int, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Responder:
    return lambda request: httpx2.Response(code, headers=dict(headers or {}), content=body)


@dataclass
class Entry:
    data: bytes
    mode: str = "100644"  # "120000" = symlink (data is the link target)


@dataclass
class Repo:
    name: str
    files: dict[str, Entry] = field(default_factory=dict)
    commits: list[str] = field(default_factory=list)  # head last
    snapshots: dict[str, dict[str, Entry]] = field(default_factory=dict)  # commit sha -> files
    trees: dict[str, dict[str, Any]] = field(default_factory=dict)  # tree sha -> {"entries": [...], "paths": ...}
    counter: int = 0

    def commit(self) -> str:
        """Freeze the current files as a new head commit."""
        self.counter += 1
        root = self._tree(self.files, "")
        sha = hashlib.sha1(f"commit {self.name} {self.counter} {root}".encode(), usedforsecurity=False).hexdigest()
        self.commits.append(sha)
        self.snapshots[sha] = dict(self.files)
        self.trees[f"commit:{sha}"] = {"tree": root}
        return sha

    def _tree(self, files: Mapping[str, Entry], prefix: str) -> str:
        children: dict[str, Any] = {}
        for path, entry in files.items():
            rest = path[len(prefix):] if prefix else path
            head, _, tail = rest.partition("/")
            if tail:
                children.setdefault(head, "tree")
            else:
                children[head] = entry
        entries = []
        for name in sorted(children):
            child = children[name]
            if child == "tree":
                sub = self._tree({p: e for p, e in files.items() if p.startswith(prefix + name + "/")}, prefix + name + "/")
                entries.append({"path": name, "mode": "040000", "type": "tree", "sha": sub})
            else:
                entries.append({
                    "path": name, "mode": child.mode, "type": "blob", "sha": blob_sha(child.data), "size": len(child.data),
                })
        sha = hashlib.sha1(json.dumps(entries, sort_keys=True).encode(), usedforsecurity=False).hexdigest()
        self.trees[sha] = {"entries": entries}
        return sha

    @property
    def head(self) -> str:
        return self.commits[-1]

    def put(self, path: str, data: bytes, mode: str = "100644") -> None:
        self.files[path] = Entry(data, mode)

    def remove(self, path: str) -> None:
        self.files.pop(path, None)


class FakeGithub:
    def __init__(self, *repos: Repo, limit: int = 5000, anonymous_limit: int = 60, reset: int = 4_000_000_000) -> None:
        self.repos = {r.name.lower(): r for r in repos}
        self.limit, self.anonymous_limit, self.reset = limit, anonymous_limit, reset
        self.used = 0
        self.token = TOKEN
        self.tree_limit: int | None = None  # a recursive listing above this many entries is truncated
        self.requests: list[tuple[str, str, str, bool]] = []
        self._scripts: dict[str, list[Responder]] = {}

    # ------------------------------------------------------------------------------- scripting

    def script(self, path: str, responders: list[Responder]) -> None:
        self._scripts.setdefault(path, []).extend(responders)

    def paths(self, host: str = API_HOST) -> list[str]:
        return [p for h, p, _q, _a in self.requests if h == host]

    def count(self, fragment: str, host: str = API_HOST) -> int:
        return sum(1 for p in self.paths(host) if fragment in p)

    # ---------------------------------------------------------------------------------- handler

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        url = urlsplit(str(request.url))
        host, path = url.hostname or "", unquote(url.path)
        auth = request.headers.get("authorization", "")
        authorized = auth == f"Bearer {self.token}"
        if auth and not authorized:
            self.requests.append((host, path, url.query, False))
            return httpx2.Response(401, json={"message": "Bad credentials"})
        self.requests.append((host, path, url.query, authorized))
        queued = self._scripts.get(path)
        if queued:
            return queued.pop(0)(request)
        cap = self.limit if authorized else self.anonymous_limit
        if path != "/rate_limit":
            if self.used >= cap:
                return httpx2.Response(403, json={"message": "API rate limit exceeded"}, headers=self._rate(cap))
            self.used += 1
        response = self._route(host, path, parse_qs(url.query))
        for k, v in self._rate(cap).items():
            response.headers.setdefault(k, v)
        return response

    def _rate(self, cap: int) -> dict[str, str]:
        return {
            "x-ratelimit-limit": str(cap), "x-ratelimit-remaining": str(max(0, cap - self.used)),
            "x-ratelimit-reset": str(self.reset),
        }

    def _route(self, host: str, path: str, query: dict[str, list[str]]) -> httpx2.Response:
        if host == RAW_HOST:
            owner, name, commit, *rest = path.strip("/").split("/")
            repo = self.repos.get(f"{owner}/{name}".lower())
            snap = repo.snapshots.get(commit) if repo else None
            entry = snap.get("/".join(rest)) if snap else None
            if entry is None:
                return httpx2.Response(404, text="404: Not Found")
            return httpx2.Response(200, content=entry.data, headers={"content-type": "application/octet-stream"})
        if path == "/rate_limit":
            return httpx2.Response(200, json={"resources": {"core": {"limit": self.limit}}})
        parts = path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "repos":
            return httpx2.Response(404, json={"message": "Not Found"})
        repo = self.repos.get(f"{parts[1]}/{parts[2]}".lower())
        if repo is None:
            return httpx2.Response(404, json={"message": "Not Found"})
        kind, rest = parts[3], parts[4:]
        if kind == "commits" and rest:
            ref = rest[0]
            sha = repo.head if ref in ("HEAD", "main", "master") else (ref if ref in repo.snapshots else None)
            if sha is None:
                return httpx2.Response(404, json={"message": "No commit found for the ref"})
            return httpx2.Response(200, json={"sha": sha, "commit": {"tree": {"sha": repo.trees[f"commit:{sha}"]["tree"]}}})
        if kind == "git" and rest[:1] == ["trees"] and len(rest) == 2:
            tree = repo.trees.get(rest[1])
            if tree is None:
                return httpx2.Response(404, json={"message": "Not Found"})
            if "recursive" in query:
                flat = self._flatten(repo, rest[1], "")
                if self.tree_limit is not None and len(flat) > self.tree_limit:
                    return httpx2.Response(200, json={"sha": rest[1], "tree": flat[: self.tree_limit], "truncated": True})
                return httpx2.Response(200, json={"sha": rest[1], "tree": flat, "truncated": False})
            return httpx2.Response(200, json={"sha": rest[1], "tree": tree["entries"], "truncated": False})
        if kind == "contents" and rest:
            ref = (query.get("ref") or ["HEAD"])[0]
            sha = repo.head if ref == "HEAD" else ref
            snap = repo.snapshots.get(sha)
            if snap is None:
                return httpx2.Response(404, json={"message": "No commit found for the ref"})
            wanted = "/".join(rest)
            entry = snap.get(wanted)
            if entry is None:
                if any(p.startswith(wanted + "/") for p in snap):
                    return httpx2.Response(200, json=[{"type": "dir", "path": wanted}])
                return httpx2.Response(404, json={"message": "Not Found"})
            if entry.mode == "120000":
                return httpx2.Response(200, json={"type": "symlink", "path": wanted, "sha": blob_sha(entry.data), "size": len(entry.data), "target": entry.data.decode()})
            body = {"type": "file", "path": wanted, "sha": blob_sha(entry.data), "size": len(entry.data)}
            if len(entry.data) <= INLINE:
                body.update(encoding="base64", content=encodebytes(entry.data).decode())
            else:
                body.update(encoding="none", content="")
            return httpx2.Response(200, json=body)
        return httpx2.Response(404, json={"message": "Not Found"})

    def _flatten(self, repo: Repo, tree_sha: str, prefix: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for e in repo.trees[tree_sha]["entries"]:
            out.append({**e, "path": prefix + e["path"]})
            if e["type"] == "tree":
                out.extend(self._flatten(repo, e["sha"], prefix + e["path"] + "/"))
        return out


# ------------------------------------------------------------------------------ the SDK-like repository

FAMILIES = ("am263x", "am243x", "am64x")


def sdk_like_repo(name: str = "example-org/sdk-core") -> Repo:
    """30 documents in a nested layout like the Acme SDK repositories, plus `docs/api_guide/current.md` (a symlink) and
    `docs/big/huge.md` (about 2 MiB, above a test's `max_bytes`), committed as the head."""
    repo = Repo(name)
    n = 0
    for family in FAMILIES:
        for i in range(4):
            repo.put(f"docs/api_guide/device/{family}/driver_{i}.md", f"# {family} driver {i}\n\nThe {family} driver {i} API.\n".encode())
            n += 1
        repo.put(f"docs/api_guide/device/{family}/boot.rst", f"{family} boot\n{'=' * 10}\n\nBoot flow of {family}.\n".encode())
        repo.put(f"docs/api_guide/device/{family}/config.yaml", f"device: {family}\nclock_mhz: 400\n".encode())
        repo.put(f"docs/pdf/{family}_trm.pdf", pdf_bytes(f"{family} TRM"))
        n += 3
    for i in range(30 - n):
        repo.put(f"docs/guides/guide_{i}.md", f"# Guide {i}\n\nSteps {i}.\n".encode())
    repo.put("docs/api_guide/current.md", b"device/am263x/driver_0.md", mode="120000")
    repo.put("docs/big/huge.md", b"# huge\n" + b"lorem ipsum " * 180_000)
    repo.put("src/main.c", b"int main(void) { return 0; }\n")
    repo.commit()
    return repo
