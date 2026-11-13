# eap-knowledge

Pure, deterministic extraction and chunking for knowledge base documents. No I/O: the output is a
function of the content, the document title and source URL, and the chunking config.

- `extract(content, mime_type, *, title="", source_url=None) -> Document`: Markdown
  (`markdown-it-py`, so `#` inside code fences is not a heading), HTML (stdlib `html.parser`;
  `script`, `style`, `nav`, `noscript`, `template` and `svg` are dropped) and plain text.
- `chunk(document, config) -> list[Chunk]`: merge sections under `min_tokens` into the next one,
  split sections over `max_tokens` (paragraphs, lines, sentences, words, then characters) with
  `overlap_tokens` overlap, and build the embed text
  `"path: <doc title> > <h1> > <h2>\n\ntitle: <heading>\n\n<text>"`, its sha256 and the anchor URL
  `source_url#slug(heading)`.
- Tokens are approximated as `ceil(len(text) / 4)`, never 0 for non-empty text.

Imports only the standard library, `eap_schemas` and `markdown_it`.
