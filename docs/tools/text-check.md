# `text.check`

`text.check` cleans a text and runs mechanical checks on it before it is published. It is pure, needs no approval,
and is in `EAP_ALLOWED_TOOLS` and the Architect's tool list. Every rule is an argument; the platform knows no
publisher, language policy or term list.

## Arguments

| name | type | |
| --- | --- | --- |
| `text` | string, at most 20000 characters | the text to check (usually an agent's answer) |
| `source` | string, optional | material the text must not copy |
| `rules` | object | the checks below; each is off unless set |

Rules: `min_graphemes`, `max_graphemes` (integers); `forbid_urls`, `forbid_hashtags`, `forbid_emoji`,
`forbid_simplified_chinese` (booleans); `forbid_terms[]` (up to 200; compared after NFKC with whitespace removed,
case counts), `forbid_terms_ci[]` (the same, case ignored); `rumour_terms[]` with `attribution_terms[]` (when any
rumour term appears, one attribution term must too); `min_han_ratio` (0 to 1: the share of Han characters among
letters); `max_verbatim_run` (a run of this many characters or more shared with `source`, after NFKC and without
punctuation, symbols and whitespace, is a copy).

## Result

`{ok, text, reason, graphemes}`. `text` is the cleaned text even when a check fails: think-tags removed, line breaks
joined, a leading `摘要：` and one wrapping pair of quotes removed. `graphemes` counts extended grapheme clusters.
`reason` is the first failed check, in this order: `empty`, `too_long`, `too_short`, `url`, `hashtag`, `emoji`,
`simplified`, `forbidden_term`, `unattributed_rumour`, `not_chinese`, `verbatim`; `null` when `ok`.

## Example

```json
{
  "text": "the draft to check",
  "source": "the material it must not copy",
  "rules": {
    "min_graphemes": 20, "max_graphemes": 300, "forbid_urls": true, "forbid_hashtags": true, "forbid_emoji": true,
    "forbid_simplified_chinese": true, "forbid_terms": ["a banned word"],
    "rumour_terms": ["allegedly"], "attribution_terms": ["according to reports"],
    "min_han_ratio": 0.5, "max_verbatim_run": 15
  }
}
```

Gate on it with a `branch` step: `{"op": "eq", "left": {"$ref": "steps.check.output.ok"}, "right": true}`.
