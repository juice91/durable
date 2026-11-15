# Importing a directory into a knowledge base

`scripts/kb_import.py` uploads a documentation tree (reStructuredText, PDF, Markdown, YAML, JSON,
HTML and plain text) into a knowledge base through the public API. Nothing is converted
beforehand: the API stores each file as it is, and the worker extracts and chunks it.

Run it again after the tree changes. Unchanged files are no-ops; a changed file becomes a new
version of its document and is ingested again.

```sh
export EAP_API_TOKEN=dev:acme:alice            # or: --token-file ~/.config/eap/token
uv run python scripts/kb_import.py ~/sdk/docs --kb <kb_id> --dry-run
uv run python scripts/kb_import.py ~/sdk/docs --kb <kb_id> \
    --metadata-map map.yaml \
    --source-url-template 'https://docs.example.com/sdk/{path_no_ext}.html'
```

The knowledge base must already exist; create it in Studio or with `POST /knowledge-bases`. Set
its extraction options when you create it, because the importer does not change them. Two examples:
- `chunking.extract.markdown_flavor: doxygen` for Doxygen-generated Markdown;
- `chunking.extract.rst_config` for `ifconfig` conditions.

## What each file becomes

| | |
|---|---|
| **Which files** | Every file under the directory whose extension is in the format registry (`eap_knowledge.formats`). Extensions are not case-sensitive. Other files are counted as `skipped: unsupported type`. Unless you pass `--include-hidden`, dot-files are counted as `skipped: hidden`, and dot-directories such as `.git` are not entered and are counted once each as `hidden directory`. Symlinked directories are not followed, and a symlinked file is imported only when its target is inside the tree. A file over `--max-file-bytes` (default 100 MiB, the largest cap the API can be configured with) fails without being read. The file is read through one open descriptor and checked against the scan's device and inode, so a path swapped after the scan (a symlink pointing out of the tree, for example) fails instead of being read. |
| **MIME type** | The canonical type for the file's extension: `.rst`/`.rest` → `text/x-rst`; `.pdf` → `application/pdf`; `.md`/`.markdown` → `text/markdown`; `.yaml`/`.yml` → `application/yaml`; `.json` → `application/json`; `.html`/`.htm` → `text/html`; `.txt`/`.text` → `text/plain`. |
| **Document key** | The path relative to the directory, in Unicode NFC (so a macOS NFD checkout and a Linux NFC one get the same keys), with `/` written as `~`: `source/devices/AM62X/linux.rst` becomes `source~devices~AM62X~linux.rst`. A document key is a single URL path segment. `--key-prefix sdk~` puts the prefix in front of every key. A prefix is a whole key segment: `--key-prefix sdk` is used as `sdk~` (the CLI says so), so `sdk2~…` documents never fall under it. A key over 256 characters keeps its first 243 characters and ends in `~` plus 12 hex digits of its hash, so it stays the same from run to run. If two files would get the same key (`a/b.md` and `a~b.md`), both fail. |
| **Title** | The relative path. |
| **Content** | Text formats are sent as UTF-8 `content`. A file that is not valid UTF-8 fails without being uploaded. PDF is sent as `content_base64`. |
| **Source URL** | Optional. `--source-url-template` with the placeholders below, each percent-encoded. Citations link to it: the worker adds `#<section anchor>` for rst and `#page=N` for PDF. |
| **Metadata** | The first matching rule of `--metadata-map` (optional), plus the importer's stamp on every document: `import_tool: kb_import` and `import_prefix: <the key prefix>`. `--delete-missing` uses the stamp to delete only documents this CLI created under the same prefix. The two keys are reserved in the map, which may hold at most 14 keys (the API allows 16). Documents imported before the stamp existed get it, and so a new version, on their next import. |

**Source-URL placeholders,** for `docs/devices/AM62X/linux.rst` with key `docs~devices~AM62X~linux.rst`:

| Placeholder | Value |
|---|---|
| `{path}` | `docs/devices/AM62X/linux.rst` |
| `{path_no_ext}` | `docs/devices/AM62X/linux` |
| `{dir}` | `docs/devices/AM62X` (empty at the top level) |
| `{name}` | `linux.rst` |
| `{stem}` | `linux` |
| `{ext}` | `rst` |
| `{key}` | the document key, percent-encoded |

The template must start with `http://` or `https://`. An unknown placeholder stops the import before
it uploads anything.

## The metadata map

The map is a YAML (or JSON) mapping from glob to flat metadata, read in file order. **The first
matching glob wins**, and rules are not merged, so put specific globs before general ones. Values
must be a string, a number or a boolean. The limits are:
- at most 16 keys per rule;
- the key `eap` is reserved.

Agents filter retrieval on these values.

Glob rules:
- `**` matches any number of directories, including none;
- `*` and `?` match within one path segment;
- `[...]` is a character class (`[!...]` negates it);
- a glob without `/` matches the file name in any directory (`*.pdf`).

```yaml
source/devices/AM62X/**: {device: am62x}
"*.pdf": {kind: manual}
```

The equivalent list form is also accepted:

```yaml
rules:
  - match: source/devices/AM62X/**
    metadata: {device: am62x}
```

A map with a nested value, a list or a null value is refused before anything is uploaded.

## Options

| Option | |
|---|---|
| `--kb ID` | The knowledge base. Required. |
| `--api-url URL` | Default: `$EAP_API_URL`, else `http://localhost:8000/api/v1`. |
| `--token-file PATH` | Read the bearer token from the first non-empty line of this file. Without this option, the token comes from `$EAP_API_TOKEN`. The token is never taken from the command line and never printed: it is scrubbed from every message and from the JSON report. |
| `--include GLOB`, `--exclude GLOB` | Repeatable. Only files matching an include, and no exclude, are imported. |
| `--include-hidden` | Also import dot-files and dot-directories. |
| `--key-prefix P` | Put `P` (completed with `~` if needed) in front of every key, and stamp it as `import_prefix`. `--delete-missing` needs it (unless `--delete-scope all`) and only deletes documents stamped with this prefix, so several trees can share one knowledge base, even with nested prefixes (`mcu~` never deletes the `mcu~am263~` tree's documents). |
| `--source-url-template T` | See above. |
| `--metadata-map FILE` | See above. |
| `--concurrency N` | Files read and uploaded at the same time: 1 to 32, default 4. |
| `--dry-run` | Uploads and deletes nothing. It prints the files per format, and an estimate of what would be created, what would start a new version and what would be unchanged. The estimate compares each file's content hash, MIME type, title, source URL and metadata with the documents the API lists. With no token, it counts files only. |
| `--delete-missing` | Delete documents whose file is no longer in the tree. Without this option, missing documents are only reported. See "Deleting missing documents" below for the safety rules. |
| `--delete-scope prefix\|all` | What `--delete-missing` may delete. `prefix` (the default) needs `--key-prefix` and deletes only documents this CLI stamped with that prefix. `all` makes every document of the knowledge base a candidate, including Studio uploads and other trees' documents: use it only for a knowledge base that holds this one tree. |
| `--max-delete N\|all` | Allow `--delete-missing` to delete up to `N` documents (default 50), or any number with `all`. The percent rule still applies. |
| `--max-delete-percent P` | Refuse a deletion over `P` % of the documents in the delete scope. Default 20; 100 lifts it. Independent of `--max-delete`. |
| `--yes-delete-all` | Lift both limits. |
| `--max-file-bytes N` | Fail files over `N` bytes without reading them. Default 104857600 (100 MiB). |
| `--retry-failed` | Start a new ingest for unchanged documents whose last ingest failed. Without it, they are counted as `unchanged but their last ingest failed`. |
| `--report-json PATH` | Also write the report as JSON: per-document results, run ids, reasons, missing and deleted keys. Checked before anything is uploaded: its directory must exist and be writable (it is never created); otherwise exit 2. |
| `--timeout S` | Per-request timeout. Default 300 s. |

**The report** has one line per category:
- `created`;
- `new version`, which started an ingest run;
- `unchanged`, the API's no-op;
- `re-ingest started`;
- `failed`, with each failure's reason, which is the API's message, such as `HTTP 413: the document is too large …`.

It then lists every document missing from the tree (all of them, not a sample). With
`--retry-failed`, a re-ingest that finds the document already ready (200) or already being
ingested (409) is reported as `unchanged` with that reason, not as a failure.

**Deleting missing documents.** `--delete-missing` is fail-safe:
- **Scope.** It needs `--key-prefix`; without one, the whole knowledge base would be in scope, so
  that takes an explicit `--delete-scope all`. A document is a candidate only when:
  - its key starts with the key prefix;
  - its metadata carries this CLI's stamp with the same prefix (`import_tool: kb_import`,
    `import_prefix`), unless `--delete-scope all`. Gone documents without it (Studio uploads,
    another tree's) are listed as `kept, not created by kb_import under this prefix`;
  - no file under the directory has its key (filtered, unsupported, hidden and failed files
    included);
  - the path the key decodes to (`~` read as `/`) passes this run's `--include`, `--exclude` and
    hidden rules, so files left out by the filters are never "missing".
- **Shortened keys.** A key shortened with a hash suffix (over 256 characters) cannot be checked
  against the tree. It is never deleted; it is listed as `kept, shortened keys that cannot be
  checked`, so you can delete it by hand.
- **Unreadable directories.** A directory the walk cannot list (permissions, a network-share
  error, removed mid-walk) hides its files, which would look missing. Each one is reported as
  `UNREADABLE <path>: <reason>`, every delete is skipped, and the exit status is 1.
- **Size.** It refuses, before uploading anything (exit 2), a deletion of more than 50 documents
  (`--max-delete`) or of more than 20 % of the documents in the scope (`--max-delete-percent`).
  The two rules are independent: raising one never lifts the other; `--yes-delete-all` lifts both.
  It prints every key it would have deleted. A wrong directory (one level too deep changes every
  key) is caught this way. The dry run reports the same refusal, lists exactly the keys that
  would be deleted, and also exits 2, so it can gate a CI job.
- **Order.** Uploads run first; the deletes run only if every file uploaded (or was unchanged).
  When any file failed, the deletes are skipped, the report says so, and the exit status is 1.
  A file that always fails (empty, not UTF-8, too large, a key collision) therefore blocks
  `--delete-missing` on every run: `--exclude` it to let the deletes run.
- **Empty tree.** A directory that produces no documents never deletes anything.

**Document cap.** A knowledge base holds at most 2,000 documents. Before uploading, the CLI adds the
new keys to the documents already there; over the cap, it stops (exit 2; the dry run only warns).
Deletions run after the uploads, so a renamed tree on a nearly full knowledge base needs room first.

**Retries.** An answer of 429, 502, 503 or 504, or a connection error, is retried up to five times
(six attempts). The wait is the server's `Retry-After` (in seconds, at most 120) plus up to 1 s of
jitter, or else 1, 2, 4, 8 and 16 s (at most 30), half fixed and half random, so parallel uploads
do not retry in lockstep. A retried upload keeps its `run_id`, so a 503 from a failed enqueue
re-queues the same run instead of starting a second one.

**Exit status.**
- 0: everything succeeded;
- 1: some documents or deletions failed, a directory could not be read, or the deletes were skipped because of either;
- 2: a usage or configuration error, which stops the import before it uploads anything. The causes are an option, the map, the token (including one passed on the command line, which is refused without being echoed; argparse errors never show option values, and the `--token-file` path is not printed), a bad `--api-url`, `--delete-missing` without `--key-prefix` or `--delete-scope all`, an unknown knowledge base, the document cap, a refused deletion (also in a dry run), or an unreachable API.

## Example: the TI SDK documentation

The TI corpus was first imported by a separate pre-processing script, which mapped paths to devices
in code. The importer does the same job with a map file kept in the TI project, not in this
repository. Import each SDK into its own knowledge base, from its documentation root, so that both
the globs and `{path_no_ext}` are relative to that root.

**Processor SDK** (`processor-sdk-doc`, reStructuredText, device pages under
`source/devices/<DEVICE>/`). Save as `~/ti-rag/import/map-processor.yaml`. Each glob appears only
once, and the map is evaluated top to bottom:

```yaml
# Device-specific pages first, then the rest of the SDK.
devices/AM62X/**:  {sdk_family: processor-sdk, device: AM62x}
devices/AM62AX/**: {sdk_family: processor-sdk, device: AM62Ax}
devices/AM62PX/**: {sdk_family: processor-sdk, device: AM62Px}
devices/AM62LX/**: {sdk_family: processor-sdk, device: AM62Lx}
devices/AM62DX/**: {sdk_family: processor-sdk, device: AM62Dx}
devices/AM64X/**:  {sdk_family: processor-sdk, device: AM64x}
devices/AM65X/**:  {sdk_family: processor-sdk, device: AM65x}
devices/AM335X/**: {sdk_family: processor-sdk, device: AM335x}
devices/AM437X/**: {sdk_family: processor-sdk, device: AM437x}
devices/AM57X/**:  {sdk_family: processor-sdk, device: AM57x}
devices/AM67A/**:  {sdk_family: processor-sdk, device: AM67A}
devices/DRA821A/**: {sdk_family: processor-sdk, device: DRA821A}
devices/J7_Family/**: {sdk_family: processor-sdk, device: J7}
# The PDFs in the tree (files/) are a tool guide and training slides, not manuals.
files/*.pdf:       {sdk_family: processor-sdk, kind: training}
"**":              {sdk_family: processor-sdk}
```

Create the knowledge base with `chunking.extract.rst_config: {CONFIG_part_variant: AM62X}` to keep
only that variant's `ifconfig` text, or without it to keep every variant's text. Then:

```sh
cd ~/orca/projects/pydanticOS
uv run python scripts/kb_import.py ~/ti-rag/repos/processor-sdk-doc/source \
    --kb <processor_kb_id> --key-prefix 'processor-sdk~' \
    --metadata-map ~/ti-rag/import/map-processor.yaml \
    --source-url-template 'https://software-dl.ti.com/processor-sdk-linux/esd/AM62X/latest/exports/docs/{path_no_ext}.html' \
    --dry-run
```

The source-URL template is illustrative: TI publishes the Processor SDK documentation per device
and release, so use the URL of the build you import. Drop `--dry-run` to upload.

**MCU+ SDK** (`mcupsdk-core`, Doxygen-flavoured Markdown, device pages under
`api_guide/device/<device>/`). Create its knowledge base with
`chunking.extract.markdown_flavor: doxygen`, and save `~/ti-rag/import/map-mcu.yaml`:

```yaml
api_guide/device/am64x_am243x/**:   {sdk_family: mcu-plus-sdk, device: AM64x_AM243x}
api_guide/device/am243x/**:         {sdk_family: mcu-plus-sdk, device: AM243x}
api_guide/device/am64x/**:          {sdk_family: mcu-plus-sdk, device: AM64x}
api_guide/device/am261x/**:         {sdk_family: mcu-plus-sdk, device: AM261x}
api_guide/device/am263px/**:        {sdk_family: mcu-plus-sdk, device: AM263Px}
api_guide/device/am263x/**:         {sdk_family: mcu-plus-sdk, device: AM263x}
api_guide/device/am273x_awr294x/**: {sdk_family: mcu-plus-sdk, device: AM273x_AWR294x}
api_guide/device/am273x/**:         {sdk_family: mcu-plus-sdk, device: AM273x}
api_guide/device/awr294x/**:        {sdk_family: mcu-plus-sdk, device: AWR294x}
api_guide/device/am62x/**:          {sdk_family: mcu-plus-sdk, device: AM62x}
api_guide/device/am65x/**:          {sdk_family: mcu-plus-sdk, device: AM65x}
"**":                               {sdk_family: mcu-plus-sdk}
```

```sh
uv run python scripts/kb_import.py ~/ti-rag/repos/mcupsdk-core/docs_src/docs \
    --kb <mcu_kb_id> --key-prefix 'mcu~' --metadata-map ~/ti-rag/import/map-mcu.yaml
```

Run the same commands again after `git pull` in the repositories: unchanged pages report
`unchanged`, and edited pages start new versions. Add `--delete-missing` to remove pages the SDK
dropped (more than 50 pages needs `--max-delete N`, more than 20 % of the SDK's documents
`--max-delete-percent P`). The key
prefix keeps that deletion to this SDK's documents.

## Runbook: the TI quality check (RAG P1 part 1, manual)

This check is manual, and it runs against the live stack, so it is not in the automated suite
(P1.A7). Run it once part 1 is deployed on the dev stack: the API on :8000, its worker, and the
Studio LiteLLM gateway serving `Qwen3-VL-Embedding-8B-mlx-8bit`. It imports the four TI SDKs again
with the native extractors (rst, PDF, Doxygen Markdown, YAML) into **new** knowledge bases, so the
old pre-processed ones stay as the baseline. It then scores both with the 16-question set in
`~/ti-rag/import/quality.py`. Each question scores 0, 1 or 2, so the maximum is 32.

- **Baseline:** 26/32 (the knowledge bases imported by `~/ti-rag/import/ti_import.py`).
- **Target:** ≥ 29/32, and no question that scored 2 on the baseline scores less.
- **Misses** are listed in the part 1 PR.

**0. One change to `quality.py` first.** Its source-page check reads `metadata.path`, which
`ti_import.py` set and this importer does not. Every hit carries its `document_key`, which is the
path with `/` written as `~` (see above), and the question regexes match it as is. In `rank_of`,
replace

```python
text = h.get("content") or "" if field == "content" else (h.get("metadata") or {}).get("path", "")
```

with

```python
text = h.get("content") or "" if field == "content" else (
    (h.get("metadata") or {}).get("path") or h.get("document_key") or "")
```

The baseline knowledge bases still score through `metadata.path`, so their numbers do not change.

**1. Record the baseline** on the same stack, so both scores use the same gateway and model:

```sh
cd ~/ti-rag/import
q() { uv run --no-project --python 3.13 --with docutils --with pypdf --with 'pydantic>=2' \
        --with markdown-it-py==4.3.0 python quality.py "$@"; }
q mcu processor c2000 zephyr | tee ~/ti-rag/import/logs/quality-baseline.md
```

**2. Create the four knowledge bases** with tenant `acme`, the tenant `quality.py` searches. Only
the MCU+ one needs an extraction option:

```sh
export EAP_API_TOKEN=dev:acme:ti-import
MODEL=litellm:Qwen3-VL-Embedding-8B-mlx-8bit
kb() {  # kb <name> [chunking JSON] -> prints the id
  curl -sf -X POST http://127.0.0.1:8000/api/v1/knowledge-bases \
    -H "Authorization: Bearer $EAP_API_TOKEN" -H 'Content-Type: application/json' \
    -d "{\"name\": \"$1\", \"embedding_model\": \"$MODEL\"${2:+, \"chunking\": $2}}" | jq -r .id
}
PROC=$(kb 'TI Processor SDK (P1 native)')
MCU=$(kb 'TI MCU+ SDK (P1 native)' '{"extract": {"markdown_flavor": "doxygen"}}')
C2K=$(kb 'TI C2000Ware (P1 native)')
ZEP=$(kb 'TI SimpleLink Zephyr (P1 native)')
echo "$PROC $MCU $C2K $ZEP"
```

New knowledge bases use chunker v2. Leave out `rst_config`, because the baseline kept every
`ifconfig` variant too.

**3. Import each SDK** from this repository's checkout on the dev stack. Use the maps from the
example above for the Processor SDK and MCU+. C2000Ware needs a device map, because question
c2000 #2 filters on `device: F2837xD`. Save it as `~/ti-rag/import/map-c2000.yaml`:

```yaml
"**/f280013x/**": {sdk_family: c2000ware, device: F280013x}
"**/f280015x/**": {sdk_family: c2000ware, device: F280015x}
"**/f28002x/**":  {sdk_family: c2000ware, device: F28002x}
"**/f28003x/**":  {sdk_family: c2000ware, device: F28003x}
"**/f28004x/**":  {sdk_family: c2000ware, device: F28004x}
"**/f2802x/**":   {sdk_family: c2000ware, device: F2802x}
"**/f2802x0/**":  {sdk_family: c2000ware, device: F2802x0}
"**/f2803x/**":   {sdk_family: c2000ware, device: F2803x}
"**/f2805x/**":   {sdk_family: c2000ware, device: F2805x}
"**/f2806x/**":   {sdk_family: c2000ware, device: F2806x}
"**/f2807x/**":   {sdk_family: c2000ware, device: F2807x}
"**/f2823x/**":   {sdk_family: c2000ware, device: F2823x}
"**/f2833x/**":   {sdk_family: c2000ware, device: F2833x}
"**/f2837xd/**":  {sdk_family: c2000ware, device: F2837xD}
"**/f2837xs/**":  {sdk_family: c2000ware, device: F2837xS}
"**/f2838x/**":   {sdk_family: c2000ware, device: F2838x}
"**/f28p55x/**":  {sdk_family: c2000ware, device: F28P55x}
"**/f28p65x/**":  {sdk_family: c2000ware, device: F28P65x}
"**":             {sdk_family: c2000ware}
```

Then import. Each command is first run with `--dry-run`, which checks the counts and uploads
nothing:

```sh
cd ~/orca/projects/pydanticOS
R=~/ti-rag/repos; M=~/ti-rag/import
NOT_DOCS=(--exclude 'LICENSE*' --exclude 'CONTRIBUTING*' --exclude 'SECURITY*' --exclude 'CODE_OF_CONDUCT*')
uv run python scripts/kb_import.py $R/processor-sdk-doc/source --kb $PROC --key-prefix 'processor-sdk~' \
    --metadata-map $M/map-processor.yaml "${NOT_DOCS[@]}" --report-json $M/logs/p1-processor.json
uv run python scripts/kb_import.py $R/mcupsdk-core/docs_src/docs --kb $MCU --key-prefix 'mcu~' \
    --metadata-map $M/map-mcu.yaml "${NOT_DOCS[@]}" --report-json $M/logs/p1-mcu.json
uv run python scripts/kb_import.py $R/c2000ware-core-sdk --kb $C2K --key-prefix 'c2000~' \
    --metadata-map $M/map-c2000.yaml "${NOT_DOCS[@]}" --report-json $M/logs/p1-c2000.json
uv run python scripts/kb_import.py $R/simplelink-zephyr --kb $ZEP --key-prefix 'zephyr~' \
    "${NOT_DOCS[@]}" --report-json $M/logs/p1-zephyr.json
```

- **What is imported.** Only `mcupsdk-core`: the baseline left out `mcu_plus_sdk_am263x` (its
  licence is not reviewed yet), so leave it out here too.
- **Failures.** A document that fails (an extraction code in the report, or `error_code` /
  `error_reason` on the document in Studio) is a finding. List it in the PR.
- **Retries.** A transient failure, such as the gateway timing out, is retried with
  `--retry-failed` on the same command.
- **When to score.** Wait until each knowledge base's documents are all `ready` or `failed`, in
  Studio or with `GET /knowledge-bases/{id}/documents`.

**4. Score the new knowledge bases** one family at a time, each with `--kb` (`q` is the function
from step 1):

```sh
cd ~/ti-rag/import
for f in "processor:TI Processor SDK (P1 native)" "mcu:TI MCU+ SDK (P1 native)" \
         "c2000:TI C2000Ware (P1 native)" "zephyr:TI SimpleLink Zephyr (P1 native)"; do
  q "${f%%:*}" --kb "${f#*:}"
done | tee ~/ti-rag/import/logs/quality-p1.md
grep '^score' ~/ti-rag/import/logs/quality-baseline.md ~/ti-rag/import/logs/quality-p1.md
```

**5. Record the result in the PR:**

- the four family scores and their sum (the target is ≥ 29/32);
- each question whose score fell from the baseline (none is allowed for a question that scored 2);
- every question below 2, with its top-5 row from `quality-p1.md` (path > heading).

Delete the four `(P1 native)` knowledge bases afterwards, or keep them as the new baseline if they
meet the target.
