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

Every licence in this file is MIT, ISC, Apache-2.0 or OFL-1.1, except axe-core (MPL-2.0), which
is a development and test dependency only and is not shipped in the Studio build.
