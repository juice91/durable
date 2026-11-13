# Third-party notices

Studio (`apps/studio`) depends on the open-source packages below. Each is used under its own
licence; the full licence text ships with the package in `node_modules/<package>/LICENSE`. No
source from these projects is copied into this repository except where a section below says so.

## Studio UI kit (milestone Studio layout, SL.2)

| Package | Version | Licence |
|---|---|---|
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
| lucide-react | 1.65.0 | ISC |
| react-resizable-panels | 4.15.3 | MIT |
| sonner | 2.0.8 | MIT |
| tailwind-merge | 3.7.0 | MIT |
| axe-core (development and tests only, not shipped) | 4.14.0 | MPL-2.0 |

class-variance-authority is distributed under the Apache License 2.0
(https://www.apache.org/licenses/LICENSE-2.0); Copyright Joe Bell. It is used unmodified as a
dependency.

The `components/ui/*` wrappers follow the conventions of shadcn/ui (MIT) — class-variance-authority
variants and a `cn()` helper — but are written for this project; no shadcn/ui source is copied.
