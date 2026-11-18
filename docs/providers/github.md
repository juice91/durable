# Provider: GitHub (repository documents)

The adapter lives in `packages/kb-sources/src/eap_kb_sources/providers/github/`; `provider = "github"` is a value in connection and
source rows (source kind `repository_docs`, connection kind `api_token`), never a tool or route. It turns the documentation files
of a repository (for the TI SDK corpus: `processor-sdk-doc`, `mcupsdk-core`, `c2000ware-core-sdk`, `mcu_plus_sdk_am263x`,
`simplelink-zephyr`) into knowledge-base documents, synchronised by the same published "KB Source Synchronization" workflow as any
source. Design: `docs/superpowers/specs/2026-11-18-kb-source-github-design.md`. The generic steps are in `docs/kb-sources.md`.

## Secrets

A public repository needs no credential: leave the connection's Token empty and the sync runs under GitHub's unauthenticated limit
(60 requests an hour). For private repositories, or a usable rate (5,000 requests an hour), store a fine-grained personal access
token with read-only "Contents" access to the repositories as a tenant secret through Studio → Settings → Connections (masked inline
create) or an agent's `secrets.request` card, and pick it as the connection's Token. Never script it into the API. The token is
resolved inside the step that calls GitHub, sent only to `api.github.com` and `raw.githubusercontent.com` and never appears in an
event, run row, checkpoint, error or log.

## Connection

```json
{"name": "github", "kind": "api_token", "base_url": "https://api.github.com",
 "config": {"provider": "github"},
 "credential_refs": {"token": "secret:<token secret name>"}}
```

`credential_refs` is `{}` for public repositories (the only key an `api_token` connection takes is `token`). The check calls
`GET /rate_limit` with the token when there is one: `connection_auth_failed` for a rejected token, `{"ok": true}` otherwise (it does
not read a repository; a wrong repository name shows up as `not_found` in the first sync). A base URL other than
`https://api.github.com` (GitHub Enterprise) is accepted but not tested; its host joins the allowlist.

## Source config

```json
{"repository": "TexasInstruments/mcupsdk-core", "ref": "main",
 "include": ["docs_src/docs/api_guide/**", "docs_src/docs/**/*.md"], "exclude": ["**/node_modules/**"],
 "map": [
   {"match": "docs_src/docs/api_guide/am263x/**", "metadata": {"sdk_family": "mcu-plus-sdk", "device": "AM263x"}},
   {"match": "**", "metadata": {"sdk_family": "mcu-plus-sdk"}}
 ],
 "max_bytes": 5242880,
 "document_policy": {"types": ["md", "rst", "html", "txt", "yaml", "yml", "json", "pdf"], "delete_missing": false},
 "key_prefix": "source:<source id>:"}
```

| key | |
|---|---|
| `repository` | `owner/name`, required |
| `ref` | branch, tag or commit; default the repository's default branch |
| `include` | path globs (at least one); `exclude`: globs removed from that set |
| `map` | ordered rules `{match, metadata}`; the **first** matching rule's metadata goes on the document (rules are not merged) |
| `max_bytes` | per-file cap, never above 5 MiB for text or 25 MiB for PDF (the knowledge base's caps) |
| `document_policy.types` | extensions to take; default all of `md rst html txt yaml yml json pdf` |
| `document_policy.delete_missing` | delete knowledge-base documents whose files disappeared (see below); default `false` |
| `key_prefix` | document-key prefix; the key is the prefix plus the path with `/` as `~` (over 256 characters: shortened with a hash) |

Globs follow the importer (`scripts/kb_import.py`): over the POSIX path from the repository root, `**` any number of directories,
`*` and `?` within one name, `[abc]` a character class; a pattern without `/` matches the file name in any directory (`*.pdf`).
Metadata values are flat (string, number, boolean); a rule may set at most 11 keys, and `source`, `source_id`, `external_id`,
`relationship`, `path` and `eap` are reserved (the adapter adds `path`, the platform stamps the rest; a document holds 16 keys).
The map is a list because the database does not keep an object's key order. Studio's Sources sheet takes the importer's YAML form and
sends this list, for example from `~/ti-rag/import/map-mcu.yaml`:

```yaml
api_guide/device/am263x/**:  {sdk_family: mcu-plus-sdk, device: AM263x}
api_guide/device/am243x/**:  {sdk_family: mcu-plus-sdk, device: AM243x}
"**":                        {sdk_family: mcu-plus-sdk}
```

## What a sync does

* `targeted` (file paths, at most 100, characters `A-Za-z0-9._/-`): each path is looked up with one contents call at the head commit
  of `ref`; it must pass include, exclude and types. Bad paths (`..`, absolute, not in scope, symlink, directory, missing) are
  counted `failed` one by one and the rest goes on.
* `incremental`: when the head commit and the source config are the same as at the last complete walk, nothing is fetched
  (`unchanged` = the files then seen). Otherwise a walk. A walk that had failures does not record its head, so the next incremental
  sync walks again.
* `full`: a walk. A walk lists the head commit's tree in one call (`git/trees?recursive=1`; if GitHub truncates it, directory by
  directory, skipping directories no include glob can reach, at most 400 calls, else the removal step is skipped), keeps the files in
  scope and processes them in segments of at most 200 files (cursor = commit, tree and offset; the commit is pinned, so a push during
  a walk does not move it). `restart` begins again at offset 0.
* Change detection is the **blob sha**: the ledger stores it (with a hash of the mapped metadata) as the document's source revision. A
  file with the same blob sha and metadata costs no request and no embedding; a changed file is a new document version. A changed
  `map` re-submits the files whose metadata changed (their text is fetched again).
* Fetching: up to 1 MiB one `contents` call (base64); above it `raw.githubusercontent.com/{repo}/{commit}/{path}`. Every body is checked
  against its blob sha. MIME comes from the extension; a PDF must carry the `%PDF` signature. Symlinks (`mode 120000`), submodules,
  paths with `..` or control characters and files over their cap are refused and counted `failed` (the rest of the walk continues).
* Documents carry the mapped metadata plus `path`, `source_url` = `https://github.com/{repo}/blob/{commit}/{path}`, and the usual
  stamps. One `kb_entities` row represents the repository (`entity_type` `repository`, identifiers `repository`, `ref`, `commit`); the
  structured store records the tree's listing hash per walk.
* **Removal**: after a complete walk, ledger rows under this source's key prefix whose file is gone (deleted, renamed, or out of
  scope) become `removed`. Only with `delete_missing` are their knowledge-base documents deleted too, and only when at most 50
  documents and 20 % of the ledger go (the importer's `--delete-missing` guard); otherwise the report carries an error entry saying the
  deletion was refused and nothing is deleted.

## Rate limits

GitHub's limit is read from the response headers. A segment that meets `x-ratelimit-remaining: 0` (or a 403/429 that says so) stops
with a cursor and the reset time (`rate: {remaining, reset}`); the next segment makes no call before the reset and returns at once,
which the run reports as `stop_reason: budget`. Run it again after the reset; it continues at the stored offset. A full walk
costs about one call per changed or new file plus two; unchanged files cost none. Unauthenticated: 60 requests an hour (a first
walk of a corpus of 1,000 files needs a token); with a token: 5,000 an hour.

## Staged live tests

1. Connection check: `POST /settings/connections/{id}/check` -> `{"ok": true}`.
2. Source on a small public repository with a narrow `include`; targeted sync of one path -> `discovered 1`, one document queued,
   `kb_ingest` completes, metadata (`path`, mapped keys) on the document and its chunks.
3. Targeted sync of 10-100 paths; then repeat it -> `unchanged` for all, no new versions.
4. `incremental` on an unchanged head -> a no-op; push a one-file change -> one new version.
5. `full` of one directory (a narrow `include`) -> walk, then repeat (all `unchanged`, no fetches).
6. Widen `include` to the corpus last; with `delete_missing` off first, then on for a directory you removed.

## Out of scope

GitLab and Bitbucket (another adapter), submodules, Git LFS pointers (they are small text files and would be ingested as such: exclude
them), private GitHub Enterprise hosts (configurable, untested), the `restart` flag re-fetching unchanged blobs (a restart re-walks
from offset 0; unchanged blobs still cost nothing).
