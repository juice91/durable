# `atproto.richtext`

`atproto.richtext` builds the text and the facets of a Bluesky post. It is pure (no I/O, no secrets), needs no
approval, and is in `EAP_ALLOWED_TOOLS` and the Architect's tool list.

On Bluesky a hashtag is only a real tag (clickable, searchable), and a URL only a real link, when the record carries
an `app.bsky.richtext.facet` whose index is in **UTF-8 bytes** (`byteStart`, `byteEnd`). Character offsets are wrong
for Chinese text and emoji, and `$template` or JMESPath cannot compute bytes, so a workflow calls this tool.

## Arguments

| name | type | |
| --- | --- | --- |
| `text` | string, at most 20000 characters | the post text |
| `append_tags` | list of strings, optional, at most 8 | tags without `#`, 1 to 64 characters, no whitespace |
| `detect` | `{tags, links}` | both default `true` |
| `max_graphemes` | integer, default 300 | the post limit |

`append_tags` are added as a new line `\n#Tag #Tag` (the text is right-trimmed first), unless the text already ends
with exactly that block, so running the tool again changes nothing. A tag that would not be a tag (all digits) is the
error `invalid_tag`.

## Result

```json
{
  "text": "台北今天放晴\n#Taiwan #twsky",
  "facets": [
    {"index": {"byteStart": 19, "byteEnd": 26}, "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": "Taiwan"}]},
    {"index": {"byteStart": 27, "byteEnd": 33}, "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": "twsky"}]}
  ],
  "graphemes": 21
}
```

Offsets index the final `text`; facets are sorted by `byteStart` and never overlap. Pass `text` and `facets` in
the `app.bsky.feed.post` record of an `http.request` `com.atproto.repo.createRecord` call.

A final text over `max_graphemes` is the error result `{error: {code: "text_too_long", graphemes, max_graphemes}}`;
nothing is truncated.

## Detection rules

They follow the official `detectFacets` of `@atproto/api` (`packages/api/src/rich-text/util.ts` in
github.com/bluesky-social/atproto) and the `app.bsky.richtext.facet` lexicon.

- **Tag**: `#` (or full-width `＃`) at the start of the text or after whitespace. The body runs to whitespace and needs a
  character that is not a digit, whitespace or punctuation, so `#2024` is no tag. Trailing punctuation, full-width
  included, is cut: `#Taiwan，` is the tag `Taiwan`. More than 64 graphemes is no tag. `「#Taiwan」` and `今天#Taiwan`
  are no tags (the `#` does not follow whitespace), and `#Taiwan真好` is one tag `Taiwan真好` (there is no whitespace to
  end it), exactly as on Bluesky: put a space after a tag.
- **Link**: an `http://` or `https://` URL at the start, after whitespace or after `(`. A trailing `.,;:!?` is cut, and a
  trailing `)` when the URL has no `(`. A tag inside a link is dropped.
