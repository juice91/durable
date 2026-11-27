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

## Example: a daily cap and a minimum gap

A posting workflow can stop posting too often with two history reads and a branch. `cap_history_24h` counts
the runs of the last 24 hours that posted, `cap_history_recent` counts the runs that posted in the last
`min_gap_hours` (a flag an earlier step derives), `cap_eval` derives the two flags, and `cap_gate` ends the
run `skipped` when either fires. The steps, before any model call:

```json
[
  {
    "id": "cap_history_24h",
    "type": "tool",
    "tool_id": "workflow.history",
    "input": {"since_hours": 24, "where": {"status": "posted"}, "select": "url", "statuses": ["completed"], "limit": 500, "distinct": false},
    "on_error": "continue",
    "next": "cap_history_recent"
  },
  {
    "id": "cap_history_recent",
    "type": "tool",
    "tool_id": "workflow.history",
    "input": {"since_hours": {"$ref": "steps.normalize_flags.output.result.min_gap_hours"}, "limit": 50},
    "next": "cap_eval"
  },
  {
    "id": "cap_eval",
    "type": "tool",
    "tool_id": "json.query",
    "input": {
      "data": {
        "n24": {"$ref": "steps.cap_history_24h.output.count"},
        "recent": {"$ref": "steps.cap_history_recent.output.count"},
        "max": {"$ref": "input.max_posts_24h"}
      },
      "expression": "{daily: n24 >= max, too_soon: recent > `0`, n24: n24, recent: recent}"
    },
    "next": "cap_gate"
  },
  {
    "id": "cap_gate",
    "type": "branch",
    "cases": [
      {"when": {"op": "eq", "left": {"$ref": "steps.cap_eval.output.result.daily"}, "right": true}, "next": "ret_daily_cap"},
      {"when": {"op": "eq", "left": {"$ref": "steps.cap_eval.output.result.too_soon"}, "right": true}, "next": "ret_too_soon"}
    ],
    "default": "post"
  },
  {"id": "ret_daily_cap", "type": "tool", "tool_id": "workflow.return", "input": {"value": {"status": "skipped_daily_cap"}, "outcome": "skipped"}},
  {"id": "ret_too_soon", "type": "tool", "tool_id": "workflow.return", "input": {"value": {"status": "skipped_too_soon"}, "outcome": "skipped"}}
]
```

`cap_gate`'s default is the workflow's next step (not shown in this fragment) — the posting step or the model
call that decides what to post. `cap_history_recent` reads its `since_hours` from `steps.normalize_flags.output.result.min_gap_hours`,
the flag an earlier step derives from the run's input.

- Place the checks before any model call: they are two platform reads and one JMESPath query, and a capped run must never spend a model call.
- Dry runs are not counted: their output carries `status: "dry_run"`, and the `where {"status": "posted"}` filter keeps only runs that posted.
- Skipped runs are not counted: `statuses` is `["completed"]`, so a run that ended `skipped` (one of these return steps, or any other skipped exit) never enters the counts.
- If the history read fails the counts are null and the run continues (fail open): the branch takes its default and posts anyway. To fail closed instead, branch on the history step's error — e.g. a branch right after the history steps with `{"op": "exists", "left": {"$ref": "steps.cap_history_24h.output.error"}}` that routes to a failure instead of continuing.
- The two return steps end the run `skipped` with `skipped_daily_cap` / `skipped_too_soon` as the output status, so the run is listed as skipped, not completed.
