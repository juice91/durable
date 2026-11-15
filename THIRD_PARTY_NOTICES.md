# Third-party notices

Studio (`apps/studio`) depends on the open-source packages below. Each is used under its own
licence; the full licence text ships with the package in `node_modules/<package>/LICENSE`. No
source from these projects is copied into this repository except where a section below says so.

## Studio UI kit (milestone Studio layout, SL.2)

Added by the milestone. Versions are the installed ones (`node_modules/<package>/package.json`).

| Package | Version | Licence |
|---|---|---|
| @fontsource-variable/inter | 5.3.0 | OFL-1.1 (package and font) |
| @fontsource-variable/jetbrains-mono | 5.3.0 | OFL-1.1 (package and font) |
| @radix-ui/react-collapsible | 1.2.1 | MIT |
| @radix-ui/react-dialog | 1.3.1 | MIT |
| @radix-ui/react-dropdown-menu | 2.2.1 | MIT |
| @radix-ui/react-popover | 1.3.1 | MIT |
| @radix-ui/react-slot | 1.5.0 | MIT |
| @radix-ui/react-switch | 1.4.1 | MIT |
| @radix-ui/react-tabs | 1.2.1 | MIT |
| @radix-ui/react-toggle-group | 1.2.1 | MIT |
| @radix-ui/react-tooltip | 1.4.1 | MIT |
| @radix-ui/react-visually-hidden | 1.2.13 | MIT |
| class-variance-authority | 0.7.1 | Apache-2.0 |
| clsx | 2.1.1 | MIT |
| cmdk | 1.1.1 | MIT |
| lucide-react | 1.65.0 | ISC (some icons MIT, see below) |
| react-resizable-panels | 4.15.3 | MIT |
| sonner | 2.0.8 | MIT |
| tailwind-merge | 3.7.0 | MIT |
| axe-core (development and tests only, not shipped) | 4.14.0 | MPL-2.0 |

### Fonts

The Studio typefaces are self-hosted from the Fontsource packages above; the package metadata
licence is `OFL-1.1` for both, and the font files are under the SIL Open Font License, Version 1.1,
as their `LICENSE` files state:

- **Inter**: "Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter)".
- **JetBrains Mono**: "Copyright 2020 The JetBrains Mono Project Authors
  (https://github.com/JetBrains/JetBrainsMono)".

The full OFL 1.1 text is in `node_modules/@fontsource-variable/inter/LICENSE` and
`node_modules/@fontsource-variable/jetbrains-mono/LICENSE` (also at https://openfontlicense.org).
The fonts are used unmodified and are not sold on their own. OFL-1.1 is not MIT, ISC or
Apache-2.0: it permits bundling and redistribution with software, but the fonts must keep their
copyright notice and licence, and a modified version may not use the reserved font names.

### Other licence notes

lucide-react is ISC ("Copyright (c) 2026 Lucide Icons and Contributors"). Its `LICENSE` also
states that a listed set of icons is derived from the Feather project and is under the MIT
License, "Copyright (c) 2013-present Cole Bemis".

class-variance-authority is distributed under the Apache License 2.0
(https://www.apache.org/licenses/LICENSE-2.0); Copyright Joe Bell. It is used unmodified as a
dependency.

The `components/ui/*` wrappers follow the conventions of shadcn/ui (MIT) — class-variance-authority
variants and a `cn()` helper — but are written for this project; no shadcn/ui source is copied.

## Other Studio runtime dependencies

Runtime dependencies of `apps/studio` that predate the Studio layout milestone and that the new
UI still uses (installed versions):

| Package | Version | Licence | Used for |
|---|---|---|---|
| next | 15.5.29 | MIT | The app framework (routing, server redirects). |
| react | 19.3.1 | MIT | UI. |
| react-dom | 19.3.1 | MIT | UI. |
| @assistant-ui/react | 0.15.35 | MIT | The agent chat thread and composer. |
| @xyflow/react | 12.12.0 | MIT | The workflow graph. |
| react-markdown | 10.1.0 | MIT | Rendering Markdown in chat messages and run event details. |
| remark-gfm | 4.0.1 | MIT | GitHub-flavoured Markdown (tables, task lists) for react-markdown. |

Every Studio licence in this file is MIT, ISC, Apache-2.0 or OFL-1.1, except axe-core (MPL-2.0), which
is a development and test dependency only and is not shipped in the Studio build.

## Python document parsers (RAG P1)

The worker parses uploaded documents with the packages below, inside its sandboxed extraction
child (`eap_knowledge.formats.*`); the API never imports them (an import-linter contract). Each is
used unmodified, as a dependency of `packages/knowledge`, pinned exactly because parser output
feeds chunk hashes.

| Package | Version | Licence | Used for |
|---|---|---|---|
| docutils | 0.23 | Public domain, with BSD-2-Clause (and BSD-3-Clause in `utils/smartquotes.py`) for the files `COPYING.rst` lists, and public domain or 0BSD (BSD-0-Clause) for `utils/_roman_numerals.py` | reStructuredText (`formats/rst.py`) |
| PyYAML | 6.0.3 | MIT | YAML (`formats/structured.py`); already a transitive dependency |
| pypdf | 6.21.4 | BSD-3-Clause | PDF text, outlines and encryption detection (`formats/pdf.py`) |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause (used under BSD-3-Clause); its dependency cffi is MIT | AES decryption of empty-password encrypted PDFs, through pypdf; already a transitive dependency |

### docutils licence check (P1.A2, 2026-11-15)

PyPI lists three licence classifiers for docutils, GPL among them. The installed 0.23 wheel was
checked file by file:

- `docutils-0.23.dist-info/licenses/COPYING.rst` places docutils in the public domain, except for
  listed files under BSD-2-Clause (and one under a revised BSD-3-Clause), and one GPL-3.0-or-later
  file: `tools/editors/emacs/rst.el`, an Emacs editor mode. `math2html.py` and its CSS came from the
  GPL eLyXer project but were relicensed by their author to BSD-2-Clause for docutils.
- The wheel's `RECORD` contains only the `docutils/` package, its `dist-info` and the `rst2*`
  console scripts: no `tools/` directory and no `.el` file. The GPL text ships only as
  `licenses/gpl-3-0.txt` (the licence file itself).
- The extractor loads the `docutils` package and 36 modules below it at runtime (`core`,
  `frontend`, `io`, `nodes`, `statemachine`, `utils`, `parsers.rst` with its states, directives
  and roles, `readers`, `transforms`, `writers.null`, `languages`). They are public domain, except
  `utils/smartquotes.py` and `utils/punctuation_chars.py`, which are BSD-2-Clause (smartquotes also
  carries SmartyPants' revised BSD-3-Clause notice), and `utils/_roman_numerals.py`, which its own
  header places in the public domain or under the Zero Clause BSD licence (0BSD), whichever is more
  permissive (`COPYING.rst` does not list it). No file in the installed `docutils` package contains
  GPL text.

Result: nothing GPL is imported or shipped by the worker. `packages/knowledge/knowledge_tests/
test_p1a2_formats.py` re-checks the installed wheel (no `.el` file, no GPL text inside the
`docutils` package, the pinned version) on every test run.

### pypdf licence check (P1.A3, 2026-11-15)

The installed 6.21.4 wheel declares `License-Expression: BSD-3-Clause` in its `METADATA`, and its
only licence file, `pypdf-6.21.4.dist-info/licenses/LICENSE`, is the BSD-3-Clause text (copyright
Mathieu Fenniak and contributors). The wheel's `RECORD` lists only the `pypdf/` package and its
`dist-info`; no file in the package mentions the GPL, LGPL or AGPL. pypdf is pure Python and has no
required dependencies. Its optional AES support uses `cryptography` 50.0.2, now declared by
`packages/knowledge` (the installed wheel's `METADATA`: `License-Expression: Apache-2.0 OR
BSD-3-Clause`, with `LICENSE`, `LICENSE.APACHE` and `LICENSE.BSD`), and the extractor never calls the external
`jbig2dec` binary (disabled through pypdf's configuration in the sandbox child). PyMuPDF (AGPL-3.0)
is not used.
