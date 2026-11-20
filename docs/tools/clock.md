# `clock.now`

`clock.now` returns the current UTC time as an ISO 8601 string. It is pure, needs no approval, and is in
`EAP_ALLOWED_TOOLS` and the Architect's tool list.

## Arguments

| name | type | default | |
| --- | --- | --- | --- |
| `offset_minutes` | integer, -10080..10080 | `0` | shift the instant before formatting (a week in either direction) |
| `parts` | boolean | `false` | return structured fields instead of a bare string |

## Result

Without `parts` (the default): a bare RFC 3339 string such as `"2026-11-18T09:30:00+00:00"`.

With `parts: true`: an object with the following fields, all UTC and strings zero-padded:

| field | example | description |
| --- | --- | --- |
| `iso` | `"2026-11-20T06:15:00+00:00"` | full ISO 8601 timestamp |
| `date` | `"2026-11-20"` | calendar date |
| `hour` | `"06"` | hour of day (00–23) |
| `minute` | `"15"` | minute of hour (00–59) |
| `hour_prefix` | `"2026-11-20T06:"` | date and hour, useful for `starts_with` checks |
| `weekday` | `"thu"` | three-letter lowercase weekday |
| `epoch_s` | `1795155300` | Unix seconds since 1970-01-01 |

## Examples

**Current time:**
```json
{}
```
returns `"2026-11-18T09:30:00+00:00"`.

**One hour ago:**
```json
{"offset_minutes": -60}
```
returns `"2026-11-18T08:30:00+00:00"`.

**Structured output:**
```json
{"parts": true}
```
returns
```json
{
  "iso": "2026-11-18T09:30:00+00:00",
  "date": "2026-11-18",
  "hour": "09",
  "minute": "30",
  "hour_prefix": "2026-11-18T09:",
  "weekday": "fri",
  "epoch_s": 1795126200
}
```

## Use case: once-per-hour check

To act once per clock hour, compare the `hour_prefix` of a stored timestamp with the current `hour_prefix`:

```json
{
  "id": "check_hour",
  "type": "tool",
  "tool_id": "clock.now",
  "input": {"parts": true}
}
```

Then in a `json.query` step:

```json
{
  "id": "same_hour",
  "type": "tool",
  "tool_id": "json.query",
  "input": {
    "data": {"newest_post": "{{ steps.newest.output }}"},
    "expression": "starts_with(newest_post, steps.check_hour.output.hour_prefix)"
  }
}
```

A branch can then route based on whether `same_hour.output.result` is `true` or `false`.
