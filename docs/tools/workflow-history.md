# `workflow.history`

`workflow.history` reads what the calling workflow's own recent runs returned (each run's output, the value of its
`workflow.return`). A workflow uses it to skip items its recent runs rejected: without it a workflow that dedupes
only by what it has posted rewrites the same refused article on every run. It is read-only, has no side effect and
needs no approval. It is in `EAP_ALLOWED_TOOLS` and the Architect's tool list, for **workflow tool steps only**: the
Architect never grants it to an agent, and an agent that calls it gets the error `not_in_workflow`.

## Arguments

| name | type | default | |
| --- | --- | --- | --- |
| `since_hours` | integer, 1..168 | `24` | the window: runs created in the last this many hours |
| `where` | object, optional | none | field → value (string, number or boolean); keeps runs whose output has that **top-level** field equal to the value |
| `select` | string, optional | the whole output | a dot path into the output (`url`, `item.url`, `items.0`); a run whose output lacks the path is left out |
| `statuses` | list of `completed`, `skipped`, `failed` | `["completed"]` | the run statuses to include |
| `limit` | integer, 1..500 | `100` | at most this many items |
| `distinct` | boolean | `true` when `select` is set, else `false` | drop repeated items (the newest occurrence stays) |

## Result

`{"items": [...], "count": N}`, newest first. Each item is the `select`ed value, or the run's whole output.
`count` is the number of items. When the items would not fit a step's output (32 KiB) the list is cut at the
newest ones that fit and the result carries `"truncated": true`; use `where`, `select` or a smaller `limit` to keep it small.
An unreadable output (not JSON) is its text.

## Scope

- Only runs of the **same workflow** as the calling run, any published version, and only the same tenant.
- Never the calling run itself.
- Dry runs are included: their output carries the same fields.
- Only top-level runs; the call is one recorded step, so a replay of the run returns the recorded result and does
  not read the history again.

## Example: the urls a check refused

```json
{
  "id": "rejected",
  "type": "tool",
  "tool_id": "workflow.history",
  "input": {"since_hours": 24, "where": {"status": "check_failed"}, "select": "url"}
}
```

returns, for runs that ended with `{"status": "check_failed", "reason": "verbatim", "url": "https://..."}`:

```json
{"items": ["https://news.example.com/a/2", "https://news.example.com/a/1"], "count": 2}
```

Pass `{"$ref": "steps.rejected.output.items"}` to `exclude_urls` of [`feed.parse`](feed-parse.md) or
[`html.extract`](html-extract.md) (links mode). See [Skip recently rejected items](../workflows/skip-rejected.md).
