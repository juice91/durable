# Skip recently rejected items

A posting workflow usually dedupes by the URLs it already posted. An article whose summary fails
[`text.check`](../tools/text-check.md) is never posted, so it stays first in the listing and is rewritten and
refused on every run (one article failed 49 times in a row, about 12 hours of wasted runs).

The reason and the URL are already in the run's return value:

```json
{"status": "check_failed", "reason": "verbatim", "url": "https://news.example.com/a/1"}
```

Read them back with [`workflow.history`](../tools/workflow-history.md) and give them to the listing's exclude list:

```json
{
  "id": "rejected",
  "type": "tool",
  "tool_id": "workflow.history",
  "input": {"since_hours": 24, "where": {"status": "check_failed"}, "select": "url"},
  "next": "listing"
}
```

```json
{
  "id": "listing",
  "type": "tool",
  "tool_id": "feed.parse",
  "input": {"text": {"$ref": "input.feed"}, "max_items": 5, "exclude_urls": {"$ref": "steps.rejected.output.items"}}
}
```

A rejected article drops out of the listing for the window (`since_hours`, at most 168), then returns for another
try. The tool sees only this workflow's own earlier runs (never the current one), dry runs included. The complete
example is `examples/workflows/skip-rejected.json`.
