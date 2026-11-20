# `html.extract`

`html.extract` reads an HTML page that `http.request` returned as `text`, without a browser: either the readable
article and its metadata, or the links of a listing page. It is pure (it fetches nothing), needs no approval, and is
in `EAP_ALLOWED_TOOLS` and the Architect's tool list. The platform knows no publisher: the page, the patterns and the
hosts are all arguments.

## Arguments

| name | type | |
| --- | --- | --- |
| `html` | string, at most 2 MiB (UTF-8) | the page; larger answers `html_too_large` |
| `base_url` | string | required; `https://` URL the page came from; relative links and images resolve against it |
| `mode` | `article` or `links` | required |
| `max_graphemes` | integer 1 to 20000 | `article`: cut for `text`, default 4000 |
| `root_selector` | CSS selector | `links`: only anchors under the matching elements; the whole page when absent |
| `include_patterns` | array of regular expressions | `links`: at least one; a link must match one |
| `exclude_patterns` | array of regular expressions | `links`: a link matching one is dropped |
| `allowed_hosts` | array of hostnames, at most 20 | hosts besides `base_url`'s that links and `og_image` may use |
| `limit` | integer 1 to 60 | `links`: default 20 |
| `exclude_urls` | array of URLs, at most 200 | `links`: a link whose normalised URL equals one (normalised the same way) is dropped |
| `exclude_terms` | array of strings, at most 50 | `links`: a link whose title contains one (case-insensitive) is dropped |

Patterns are RE2 expressions (linear time, no look-around, no back-references), at most 10 per list and 200
characters each; anything else answers `invalid_pattern`. They see the normalised URL (below).

## Result: `article`

`{title, text, published_at, canonical_url, og_image, description, lang, graphemes}`.

- `title`: `og:title`, else the first `h1`, else the `title` element. `description`: `og:description` or the
  description meta, or `""`. `lang`: the `html` element's `lang`, or `null`.
- `published_at` (ISO 8601, or `null`): `article:published_time`, else `datePublished` in JSON-LD, else the first
  `<time datetime>`.
- `canonical_url` and `og_image` are absolute https URLs or `null`. `og_image` is also `null` unless its host is
  `base_url`'s or in `allowed_hosts`: put the image CDN's host there, then use `og_image` with `http.request`'s
  `body_from_url` and the same `allowed_hosts`.
- `text`: the main readable content, one paragraph per line. A readability heuristic picks the container that holds
  most of the paragraph text (an `<article>` if there is one), after removing scripts, styles, `noscript`,
  templates, frames, SVG, forms, navigation, footers and asides; inside it, small blocks whose class or id names an
  ad, share bar, related list or comments are dropped. It is plain text: all markup is gone, and a `<` that would
  start a tag is dropped. `<think>` blocks are removed. `graphemes` counts extended grapheme clusters.

## Result: `links`

`{items: [{id, url, title, order}], dropped: {...}}` with ids `l1`, `l2`, ... in page order.

- Only **https** links are returned: `javascript:`, `data:`, `mailto:` and `http:` are dropped.
- **Same host**: a link whose host is neither `base_url`'s nor in `allowed_hosts` is dropped. The page controls its
  HTML, so this is the guard against a listing that points a workflow at an arbitrary site.
- The URL is normalised: scheme and host lower case, fragment, `utm_*` and `fbclid` query keys and a trailing slash
  dropped. Duplicates keep the first.
- `exclude_urls` and `exclude_terms` apply after the patterns and before `limit`, so `limit` counts the links kept.
  Typical use: `exclude_urls` is the links an account already posted (from Bluesky `getAuthorFeed`), `exclude_terms`
  is `["美通社", "新聞稿", "PR Newswire"]` to skip press releases. `feed.parse` has the same two filters.
- `dropped` counts what was left out: `{scheme, host, duplicate, pattern, no_title, excluded_url, excluded_term}`
  (anchors examined; scanning stops once `limit` links are kept).
- The title is the anchor's text, else its `title` or `aria-label`, else an image's `alt`; whitespace collapsed, at
  most 200 characters. For an image-only link (a thumbnail with no text, `title` or alt) the title is the headline of the enclosing card: the first heading, else the first paragraph, within three levels up, ignoring text inside other links and inside `nav`, `aside`, `footer` or `button`, and only when that card holds no other article link. An anchor still without a title is skipped.

## Errors

A result, never an exception: `{error: {code, message}}`.

| code | when |
| --- | --- |
| `html_too_large` | over 2 MiB |
| `html_unparseable` | empty, or no markup at all |
| `html_too_complex` | over 50,000 elements, or nested deeper than 150 levels |
| `no_patterns` | `links` without `include_patterns` |
| `invalid_pattern` | a pattern is not valid RE2, is over 200 characters, or there are more than 10 |
| `selector_invalid` | `root_selector` is not a CSS selector |
| `base_url_invalid` | `base_url` is not an https URL |

## Limits

The parser reads HTML5 only: it resolves no entity beyond the HTML character references, loads no DTD or URL and
runs no script. The work is bounded by the size, element and depth limits above. It is a heuristic, not a
guarantee: a page whose article is rendered by JavaScript has no text to find, and a layout with no paragraph
markup gives a weak `text`; check the result (`text.check`) before publishing.

## Example

```json
{"html": "<the text of an http.request GET>", "base_url": "https://news.example.com/section/politics",
 "mode": "links", "root_selector": "main", "include_patterns": ["^https://news\\.example\\.com/story/\\d+"],
 "exclude_patterns": ["/video/"], "limit": 10}
```
