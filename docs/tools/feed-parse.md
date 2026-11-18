# `feed.parse`

`feed.parse` turns RSS 2.0 or Atom text into numbered, filtered items. It is pure: it fetches nothing, so a workflow
gets the document with [`http.request`](http-request.md) (a feed answers as `text`) and passes `steps.<id>.output.text`
here. It is in `EAP_ALLOWED_TOOLS`, needs no approval and the Architect offers it in `architect.list_approved_tools`.

## Arguments

Exactly one of `text` and `feeds` (`feed_args_invalid` otherwise).

| name | type | |
| --- | --- | --- |
| `text` | string, at most 1 MiB | one document |
| `feeds` | 1 to 6 of `{text, label, include_terms?}` | several documents, merged and numbered once; `label` (at most 100 characters) becomes each item's `source`; `include_terms` filters that feed only |
| `max_items` | integer 1 to 50 | default 20; cut after filtering |
| `max_age_hours` | number > 0 | drops items older than this; an item without a readable date is kept |
| `include_terms` | up to 200 strings | an item must contain one (any-of, case-insensitive, over title plus summary) |
| `exclude_urls` | up to 200 strings | drops an item whose link equals one (trailing slash ignored) |
| `exclude_terms` | up to 200 strings | drops an item that contains one (case-insensitive) |
| `now` | RFC 3339 | the clock for `max_age_hours`; default the current time (`now_invalid` when unreadable) |

XML is parsed with defusedxml: a DTD or an entity (an XML bomb, an external reference) makes the document
`feed_invalid`. With `feeds`, an unreadable feed is skipped and its label is listed in `failed_feeds`; the call fails
only when no feed is readable.

## Result

```json
{
  "items": [{"id": "c1", "title": "…", "link": "https://…", "summary": "plain text, at most 400 characters",
             "published_at": "2026-11-18T08:00:00Z", "source": "Source A"}],
  "listing": "c1｜title｜excerpt (at most 120 characters)\nc2｜…",
  "count": 1,
  "dropped": {"excluded_url": 0, "old": 0, "no_term": 0, "term": 0, "malformed": 0},
  "failed_feeds": []
}
```

Items are newest first (undated last) and numbered `c1`, `c2`, … after filtering. `source` and `failed_feeds` appear
only with `feeds`. An item without a title or a link is `malformed`. Each dropped item is counted once, in the first
category that applies: malformed, excluded_url, old, no_term, term. `listing` is meant to be pasted into an agent's
prompt (`{{ steps.<id>.output.listing }}`); `items` is for `json.query`.

## Example

```json
{
  "feeds": [{"text": "<the text of an http.request GET>", "label": "Source A", "include_terms": ["keyword"]}],
  "max_age_hours": 24,
  "exclude_urls": ["https://example.com/already-posted"],
  "exclude_terms": ["term to skip"],
  "max_items": 10
}
```
