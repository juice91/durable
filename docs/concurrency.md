# Concurrency: ETag and If-Match

Two people (or a person and the Architect) can edit the same definition at the same time. Without a
guard the last writer silently overwrites the first. Every definition-class resource therefore carries a
`revision`, and an edit says which revision it was based on.

Covered resources (phase 1): agents (draft), workflows (draft), workflow schedules, eval suites and
cases, settings connections, knowledge-base sources, scorers and MCP servers. Runs, conversations,
memory, secrets (a `PUT` replaces by design), attachments and feedback are not covered.

## The contract

- **`revision`** is an integer that starts at 1 and goes up by one on every write that changes the
  row: edits (`PATCH`), publish, restore, archive and unarchive (agents, workflows), a schedule that is
  paused, resumed or stopped, an archive that pauses a workflow's schedules. A system tick that is not an
  edit of the definition (a schedule firing, a source's sync checkpoint, a connection check result) does
  **not** move it, so an open editor never goes stale because the worker ran. `updated_at` keeps its
  meaning (when the row last changed); `revision` is the concurrency token.
- **Reads.** Every single-resource `GET`, and every `PATCH` or `POST` response of these resources, has
  the header `ETag: "<revision>"` (strong, quoted integer) and the body field `revision`. List responses
  carry `revision` per item and no header.
- **Writes.** Send `If-Match: "<revision>"` on `PATCH`. The bare integer (`If-Match: 3`) and the weak form
  (`W/"3"`) are accepted too, and `*` means "whatever the current revision is" (a deliberate overwrite).
  Something that is not one ETag is `400 invalid_if_match`.
- **Stale write: `412 Precondition Failed`.** Nothing is written, and the body carries the current
  resource, exactly as `GET` returns it, so a client can diff without a second request:

  ```json
  {
    "detail": "this resource changed since you read it (revision 4, you sent 3); re-read it or send the current ETag to overwrite",
    "code": "revision_mismatch",
    "current_revision": 4,
    "current": { "id": "…", "name": "…", "revision": 4 }
  }
  ```

  The response also has the current `ETag`. To overwrite on purpose, resend with
  `If-Match: "4"` (or `*`).
- **Missing `If-Match`** is allowed for now, so scripts and MCP clients keep working. The write goes
  through, bumps the revision, and the response has
  `Warning: 299 - "write without If-Match; send the ETag you read"`.
- **`EAP_REQUIRE_IF_MATCH=1`** (API environment, default off) turns a missing header into
  `428 Precondition Required` (`{"detail", "code": "precondition_required"}`). Turn it on once your clients
  send the header.

The comparison and the increment are one statement (`UPDATE … SET revision = revision + 1 WHERE id = … AND
revision = :expected`), so two writers that read the same revision cannot both win: one gets 200, the other
412.

## Studio

Studio remembers the last `revision` it read (or saved) per resource and sends it as `If-Match` on every
edit. When a save is refused it shows 「這份內容已被他人修改」 with two actions: **重新載入** loads the
current version into the editor and drops your local edits; **覆寫** resends your save on top of the
current version.

## Architect and MCP tools

They write through the same services, with the revision they read. If somebody saved in between, the tool
reads again, applies its change to the fresh version and retries once. A second conflict is a tool error
the model can act on:

```json
{ "error": { "code": "revision_mismatch", "message": "…", "current_revision": 7 } }
```

This covers the Architect's `save_agent`, `save_workflow` and `set_workflow_schedule`, and the MCP control
tools `agent.save_draft`, `workflow.save_draft` and `workflow.schedule.set` (see
[mcp-control.md](mcp-control.md)).

## Example

```sh
# read
curl -si -H "Authorization: $TOKEN" $API/api/v1/agents/$ID | grep -i '^etag'
# ETag: "3"

# save based on it
curl -s -X PATCH -H "Authorization: $TOKEN" -H 'If-Match: "3"' -H 'Content-Type: application/json' \
  -d @spec.json $API/api/v1/agents/$ID
```
