# `json.query`

`json.query` runs a [JMESPath](https://jmespath.org) expression over any JSON value and returns `{result}`. It is
pure, needs no approval, and is in `EAP_ALLOWED_TOOLS` and the Architect's tool list.

## Arguments

| name | type | |
| --- | --- | --- |
| `data` | any JSON, at most 1 MiB | the value to query, usually `{"$ref": "steps.<id>.output"}` or a `$template` object |
| `expression` | string, at most 500 characters | JMESPath |

## Result

`{"result": <value>}`. A path that matches nothing is `{"result": null}`. Errors are `{error: {code, message}}`:

- `expression_invalid`: the expression does not parse, or a function got a value of the wrong type;
- `result_too_large`: the result is over 256 KiB; select less.

## Examples

| expression | meaning |
| --- | --- |
| `items[].title` | a projection: every title |
| `items[?id=='c3'] \| [0]` | a filter, then the first match (or `null`) |
| `(items[?id=='c3'] \| [0]) \|\| items[0]` | the match, else the first item |
| `{links: feed[].post.record.embed.external.uri, n: length(feed)}` | a multiselect hash |

To put a value from an earlier step into an expression use a `$template` input:
`{"expression": {"$template": "items[?id=='{{ steps.pick.output.picks.0 }}'] | [0]"}}`.
