# Third-Party Notices

This document lists the licenses of dependencies used by this project.

## Generated

To regenerate this file, run:

```sh
# Python dependencies (runtime only, excluding first-party packages)
uv run pip-licenses --format=markdown --with-urls | grep -v "^| eap-" | grep -v "^| eap-"

# JavaScript dependencies (production only)
pnpm -C apps/studio licenses list --prod --json > /tmp/js_licenses.json
```

Generated on 2026-11-20 19:43:24 UTC.

## Python Dependencies

| Name | Version | License | URL |
|------|---------|---------|-----|
| ag-ui-protocol | 1.0.0 | MIT | https://github.com/ag-ui-protocol/ag-ui/releases |
| aiobotocore | 3.10.0 | Apache-2.0 | https://github.com/aio-libs/aiobotocore |
| aiofile | 3.12.3 | Apache-2.0 | https://github.com/mosquito/aiofile |
| aiohappyeyeballs | 2.7.1 | Python Software Foundation License | https://github.com/aio-libs/aiohappyeyeballs |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT | https://github.com/aio-libs/aiohttp |
| aioitertools | 0.13.0 | MIT | https://aioitertools.omnilib.dev/en/latest/changelog.html |
| aiosignal | 1.4.0 | Apache Software License | https://github.com/aio-libs/aiosignal |
| alembic | 1.20.1 | MIT | https://alembic.sqlalchemy.org |
| annotated-doc | 0.0.6 | MIT | https://github.com/fastapi/annotated-doc |
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| anthropic | 2.8.0 | Apache Software License | https://github.com/anthropics/anthropic-sdk-python |
| anyio | 4.16.0 | MIT | https://anyio.readthedocs.io/en/stable/versionhistory.html |
| asgiref | 3.13.0 | BSD-3-Clause | https://github.com/django/asgiref/ |
| attrs | 26.1.0 | MIT | https://www.attrs.org/en/stable/changelog.html |
| Authlib | 1.9.5 | BSD License | https://github.com/authlib/authlib |
| beartype | 0.23.1 | MIT | UNKNOWN |
| botocore | 1.43.117 | Apache-2.0 | https://github.com/boto/botocore |
| cachetools | 8.0.0 | MIT | https://github.com/tkem/cachetools/ |
| caio | 0.12.10 | Apache-2.0 | UNKNOWN |
| certifi | 2026.10.23 | Mozilla Public License 2.0 (MPL 2.0) | https://github.com/certifi/python-certifi |
| cffi | 2.1.1 | MIT-0 | https://cffi.readthedocs.io/en/latest/whatsnew.html |
| charset-normalizer | 3.5.2 | MIT | https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md |
| click | 8.5.1 | BSD-3-Clause | https://github.com/pallets/click/ |
| croniter | 7.0.0 | MIT | https://github.com/pallets-eco/croniter |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| cyclopts | 5.4.2 | Apache-2.0 | https://github.com/BrianPugh/cyclopts |
| dbos | 3.5.0 | MIT License | UNKNOWN |
| defusedxml | 0.7.1 | Python Software Foundation License | https://github.com/tiran/defusedxml |
| dnspython | 2.9.2 | ISC | https://www.dnspython.org |
| docstring_parser | 0.18.0 | MIT License | https://github.com/rr-/docstring_parser |
| docutils | 0.23 | BSD License; GNU General Public License (GPL); Public Domain | https://docutils.sourceforge.io |
| email-validator | 2.3.0 | The Unlicense (Unlicense) | https://github.com/JoshData/python-email-validator |
| exceptiongroup | 1.3.1 | MIT License | https://github.com/agronholm/exceptiongroup/blob/main/CHANGES.rst |
| execnet | 2.1.2 | MIT | https://execnet.readthedocs.io/en/latest/ |
| fastapi | 0.162.4 | MIT | https://github.com/fastapi/fastapi |
| fastmcp-slim | 4.1.8 | Apache-2.0 | https://gofastmcp.com |
| frozenlist | 1.8.0 | Apache-2.0 | https://github.com/aio-libs/frozenlist |
| genai-prices | 0.2.3 | MIT | https://github.com/pydantic/genai-prices |
| google-re2 | 1.1.20251105 | BSD License | https://github.com/google/re2 |
| googleapis-common-protos | 1.76.0 | Apache-2.0 | https://github.com/googleapis/google-cloud-python/tree/main/packages/googleapis-common-protos |
| greenlet | 3.5.6 | MIT AND PSF-2.0 | https://greenlet.readthedocs.io |
| griffelib | 2.4.1 | ISC | https://mkdocstrings.github.io/griffe |
| grimp | 3.19 | BSD License | https://grimp.readthedocs.io/ |
| h11 | 0.16.0 | MIT License | https://github.com/python-hyper/h11 |
| h2 | 4.4.1 | MIT | https://github.com/python-hyper/h2/ |
| hpack | 4.2.0 | MIT | https://github.com/python-hyper/hpack/ |
| httpcore2 | 2.16.0 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| httpx2 | 2.16.0 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| hyperframe | 6.1.0 | MIT License | https://github.com/python-hyper/hyperframe/ |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| import-linter | 2.16 | BSD License | https://import-linter.readthedocs.io/ |
| iniconfig | 2.3.1 | MIT | https://github.com/pytest-dev/iniconfig |
| jiter | 0.19.0 | MIT | https://github.com/pydantic/jiter/ |
| jmespath | 1.1.0 | MIT License | https://github.com/jmespath/jmespath.py |
| joserfc | 1.8.0 | BSD License | https://github.com/authlib/joserfc |
| jsonref | 1.1.0 | MIT | https://github.com/gazpachoking/jsonref |
| jsonschema | 4.26.0 | MIT | https://github.com/python-jsonschema/jsonschema |
| jsonschema-path | 0.5.0 | Apache Software License | https://github.com/p1c2u/jsonschema-path |
| jsonschema-specifications | 2025.9.1 | MIT | https://github.com/python-jsonschema/jsonschema-specifications |
| lightkube | 1.0.2 | MIT | https://github.com/gtsystem/lightkube |
| lightkube-models | 1.37.0.8 | MIT | https://github.com/gtsystem/lightkube-models |
| logfire-api | 5.5.0 | MIT | UNKNOWN |
| Mako | 1.4.3 | MIT | https://www.makotemplates.org/ |
| markdown-it-py | 4.3.0 | MIT License | https://github.com/executablebooks/markdown-it-py |
| MarkupSafe | 3.0.4 | BSD-3-Clause | https://github.com/pallets/markupsafe/ |
| mcp | 2.3.0 | MIT License | https://modelcontextprotocol.io |
| mcp-types | 2.3.0 | MIT License | https://modelcontextprotocol.io |
| mdurl | 0.1.2 | MIT License | https://github.com/executablebooks/mdurl |
| msgspec | 0.22.0 | BSD-3-Clause | https://msgspec.dev |
| multidict | 7.2.1 | Apache-2.0 | https://github.com/aio-libs/multidict |
| openai | 3.49.0 | Apache-2.0 | https://github.com/openai/openai-python |
| openapi-pydantic | 0.6.0 | MIT License | https://github.com/mike-oakley/openapi-pydantic |
| opentelemetry-api | 1.45.2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api |
| opentelemetry-exporter-http-transport | 0.66b2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-http-transport |
| opentelemetry-exporter-otlp-common | 0.66b2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-common |
| opentelemetry-exporter-otlp-proto-common | 1.45.2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-proto-common |
| opentelemetry-exporter-otlp-proto-http | 1.45.2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-proto-http |
| opentelemetry-instrumentation | 0.66b2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/opentelemetry-instrumentation |
| opentelemetry-instrumentation-asgi | 0.66b2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation/opentelemetry-instrumentation-asgi |
| opentelemetry-instrumentation-fastapi | 0.66b2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation/opentelemetry-instrumentation-fastapi |
| opentelemetry-instrumentation-logging | 0.66b2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation/opentelemetry-instrumentation-logging |
| opentelemetry-proto | 1.45.2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-proto |
| opentelemetry-sdk | 1.45.2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-sdk |
| opentelemetry-semantic-conventions | 0.66b2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-semantic-conventions |
| opentelemetry-util-http | 0.66b2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/util/opentelemetry-util-http |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| pathable | 0.6.0 | Apache Software License | https://github.com/p1c2u/pathable |
| pgvector | 0.5.1 | MIT | https://github.com/pgvector/pgvector-python |
| platformdirs | 4.14.5 | MIT | https://github.com/tox-dev/platformdirs |
| pluggy | 1.7.0 | MIT License | https://github.com/pytest-dev/pluggy |
| propcache | 0.5.4 | Apache-2.0 | https://github.com/aio-libs/propcache |
| protobuf | 7.36.2 | 3-Clause BSD License | https://developers.google.com/protocol-buffers/ |
| psycopg | 3.3.8 | LGPL-3.0-only | https://psycopg.org/ |
| psycopg-binary | 3.3.8 | LGPL-3.0-only | https://psycopg.org/ |
| py-key-value-aio | 0.4.6 | Apache-2.0 | UNKNOWN |
| pycparser | 3.12 | BSD-3-Clause | https://github.com/eliben/pycparser |
| pydantic | 2.14.3 | MIT | https://github.com/pydantic/pydantic |
| pydantic-ai-slim | 2.67.0 | MIT | https://github.com/pydantic/pydantic-ai/tree/main/pydantic_ai_slim |
| pydantic-evals | 2.67.0 | MIT | https://pydantic.dev/docs/ai/evals/evals/ |
| pydantic-graph | 2.67.0 | MIT | https://pydantic.dev/docs/ai/graph/graph/ |
| pydantic-settings | 2.16.2 | MIT | https://github.com/pydantic/pydantic-settings |
| pydantic_core | 2.50.4 | MIT | https://github.com/pydantic |
| Pygments | 2.21.0 | BSD-2-Clause | https://pygments.org |
| PyJWT | 2.15.1 | MIT | https://github.com/jpadilla/pyjwt |
| pypdf | 6.21.4 | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| pyperclip | 1.11.0 | BSD License | https://github.com/asweigart/pyperclip |
| pytest | 9.2.0 | MIT | https://docs.pytest.org/en/latest/ |
| pytest-asyncio | 1.4.1.post1 | Apache-2.0 | https://github.com/pytest-dev/pytest-asyncio |
| pytest-xdist | 3.8.0 | MIT | https://github.com/pytest-dev/pytest-xdist |
| python-dateutil | 2.9.0.post0 | Apache Software License; BSD License | https://github.com/dateutil/dateutil |
| python-dotenv | 1.2.4 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| python-multipart | 0.0.33 | Apache-2.0 | https://github.com/Kludex/python-multipart |
| PyYAML | 6.0.3 | MIT License | https://pyyaml.org/ |
| referencing | 0.37.0 | MIT | https://github.com/python-jsonschema/referencing |
| regex | 2026.11.9 | Apache-2.0 AND CNRI-Python | https://github.com/mrabarnett/mrab-regex |
| requests | 2.34.2 | Apache Software License | https://github.com/psf/requests |
| rich | 15.0.0 | MIT License | https://github.com/Textualize/rich |
| rich-rst | 2.2.0 | MIT | https://wasi-master.github.io/rich-rst |
| rpds-py | 2026.9.1 | MIT | https://github.com/crate-py/rpds |
| selectolax | 1.0.2 | MIT | https://github.com/rushter/selectolax |
| six | 1.17.0 | MIT License | https://github.com/benjaminp/six |
| sniffio | 1.3.1 | Apache Software License; MIT License | https://github.com/python-trio/sniffio |
| SQLAlchemy | 2.1.8 | MIT | https://www.sqlalchemy.org |
| sse-starlette | 3.7.0 | BSD-3-Clause | https://github.com/sysid/sse-starlette |
| starlette | 1.10.0 | BSD-3-Clause | https://github.com/Kludex/starlette |
| tiktoken | 0.14.0 | MIT License | https://github.com/openai/tiktoken |
| truststore | 0.10.5 | MIT | https://github.com/sethmlarson/truststore |
| typing-inspection | 0.4.5 | MIT | https://github.com/pydantic/typing-inspection |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| uncalled-for | 0.6.1 | MIT | https://github.com/chrisguidry/uncalled-for |
| urllib3 | 2.8.0 | MIT | https://github.com/urllib3/urllib3/blob/main/CHANGES.rst |
| uvicorn | 0.54.0 | BSD-3-Clause | https://uvicorn.dev/ |
| watchfiles | 1.3.0 | MIT License | https://github.com/samuelcolvin/watchfiles |
| websockets | 17.4 | BSD-3-Clause | https://github.com/python-websockets/websockets |
| wrapt | 2.5.2 | BSD-2-Clause | https://github.com/GrahamDumpleton/wrapt |
| wsproto | 1.3.2 | MIT | https://github.com/python-hyper/wsproto/ |
| yarl | 1.25.1 | Apache-2.0 | https://github.com/aio-libs/yarl |

## JavaScript Dependencies

| Name | Version | License | URL |
|------|---------|---------|-----|
| @ag-ui/client | 1.0.3 | MIT | https://github.com/ag-ui-protocol/ag-ui#readme |
| @ag-ui/core | 1.0.3 | MIT | https://github.com/ag-ui-protocol/ag-ui#readme |
| @ag-ui/encoder | 1.0.3 | MIT | https://github.com/ag-ui-protocol/ag-ui#readme |
| @ag-ui/proto | 1.0.3 | MIT | https://github.com/ag-ui-protocol/ag-ui#readme |
| @assistant-ui/core | 0.3.35 | MIT | https://www.assistant-ui.com/ |
| @assistant-ui/generative-ui | 0.0.8 | MIT | https://www.assistant-ui.com/ |
| @assistant-ui/react | 0.15.35 | MIT | https://www.assistant-ui.com/ |
| @assistant-ui/react-ag-ui | 0.1.1 | MIT | https://www.assistant-ui.com/ |
| @assistant-ui/store | 0.3.23 | MIT | https://www.assistant-ui.com/ |
| @assistant-ui/tap | 0.9.25 | MIT | https://www.assistant-ui.com/ |
| @babel/code-frame | 7.29.7 | MIT | https://babel.dev/docs/en/next/babel-code-frame |
| @babel/compat-data | 7.29.7 | MIT | https://github.com/babel/babel#readme |
| @babel/core | 7.29.13 | MIT | https://babel.dev/docs/en/next/babel-core |
| @babel/generator | 7.29.13 | MIT | https://babel.dev/docs/en/next/babel-generator |
| @babel/helper-compilation-targets | 7.29.13 | MIT | https://github.com/babel/babel#readme |
| @babel/helper-globals | 7.29.7 | MIT | https://github.com/babel/babel#readme |
| @babel/helper-module-imports | 7.29.7 | MIT | https://babel.dev/docs/en/next/babel-helper-module-imports |
| @babel/helper-module-transforms | 7.29.13 | MIT | https://babel.dev/docs/en/next/babel-helper-module-transforms |
| @babel/helper-string-parser | 7.29.13 | MIT | https://babel.dev/docs/en/next/babel-helper-string-parser |
| @babel/helper-validator-identifier | 7.29.7 | MIT | https://github.com/babel/babel#readme |
| @babel/helper-validator-option | 7.29.7 | MIT | https://github.com/babel/babel#readme |
| @babel/helpers | 7.29.13 | MIT | https://babel.dev/docs/en/next/babel-helpers |
| @babel/parser | 7.29.13 | MIT | https://babel.dev/docs/en/next/babel-parser |
| @babel/runtime | 7.29.13 | MIT | https://babel.dev/docs/en/next/babel-runtime |
| @babel/template | 7.29.7 | MIT | https://babel.dev/docs/en/next/babel-template |
| @babel/traverse | 7.29.13 | MIT | https://babel.dev/docs/en/next/babel-traverse |
| @babel/types | 7.29.11 | MIT | https://babel.dev/docs/en/next/babel-types |
| @bufbuild/protobuf | 2.17.0 | (Apache-2.0 AND BSD-3-Clause) | https://protobufes.com/ |
| @floating-ui/core | 1.8.0 | MIT | https://floating-ui.com |
| @floating-ui/dom | 1.8.0 | MIT | https://floating-ui.com |
| @floating-ui/react-dom | 2.1.9 | MIT | https://floating-ui.com/docs/react-dom |
| @floating-ui/utils | 0.2.12 | MIT | https://floating-ui.com |
| @fontsource-variable/inter | 5.3.0 | OFL-1.1 | https://fontsource.org/fonts/inter |
| @fontsource-variable/jetbrains-mono | 5.3.0 | OFL-1.1 | https://fontsource.org/fonts/jetbrains-mono |
| @img/colour | 1.1.0 | MIT | https://github.com/lovell/colour#readme |
| @img/sharp-darwin-arm64 | 0.35.5 | Apache-2.0 | https://sharp.pixelplumbing.com |
| @img/sharp-libvips-darwin-arm64 | 1.3.4 | LGPL-3.0-or-later | https://sharp.pixelplumbing.com |
| @jridgewell/gen-mapping | 0.3.13 | MIT | https://github.com/jridgewell/sourcemaps/tree/main/packages/gen-mapping |
| @jridgewell/remapping | 2.3.5 | MIT | https://github.com/jridgewell/sourcemaps/tree/main/packages/remapping |
| @jridgewell/resolve-uri | 3.1.2 | MIT | https://github.com/jridgewell/resolve-uri#readme |
| @jridgewell/sourcemap-codec | 1.6.0 | MIT | https://github.com/jridgewell/sourcemaps/tree/main/packages/sourcemap-codec |
| @jridgewell/trace-mapping | 0.3.31 | MIT | https://github.com/jridgewell/sourcemaps/tree/main/packages/trace-mapping |
| @next/env | 15.5.29 | MIT | https://github.com/vercel/next.js#readme |
| @next/swc-darwin-arm64 | 15.5.29 | MIT | https://github.com/vercel/next.js#readme |
| @protobuf-ts/protoc | 2.11.1 | Apache-2.0 | https://github.com/timostamm/protobuf-ts |
| @radix-ui/number | 1.1.3 | MIT | https://radix-ui.com/primitives |
| @radix-ui/primitive | 1.2.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-accessible-icon | 1.2.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-accordion | 1.3.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-alert-dialog | 1.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-arrow | 1.1.17 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-aspect-ratio | 1.2.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-avatar | 1.3.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-checkbox | 1.4.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-collapsible | 1.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-collection | 1.2.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-compose-refs | 1.1.5 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-context | 1.3.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-context-menu | 2.4.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-dialog | 1.3.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-direction | 1.2.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-dismissable-layer | 1.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-dropdown-menu | 2.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-focus-guards | 1.2.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-focus-scope | 1.3.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-form | 0.3.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-hover-card | 1.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-id | 1.1.4 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-label | 2.1.17 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-menu | 2.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-menubar | 1.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-navigation-menu | 1.4.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-one-time-password-field | 0.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-password-toggle-field | 0.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-popover | 1.3.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-popper | 1.4.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-portal | 1.2.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-presence | 1.1.11 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-primitive | 2.2.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-progress | 1.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-radio-group | 1.5.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-roving-focus | 1.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-scroll-area | 1.4.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-select | 2.4.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-separator | 1.2.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-slider | 1.6.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-slot | 1.5.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-switch | 1.4.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-tabs | 1.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-toast | 1.3.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-toggle | 1.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-toggle-group | 1.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-toolbar | 1.2.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-tooltip | 1.4.1 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-use-callback-ref | 1.1.5 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-use-controllable-state | 1.3.0 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-use-effect-event | 0.0.5 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-use-escape-keydown | 1.1.6 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-use-is-hydrated | 0.1.3 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-use-layout-effect | 1.1.4 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-use-previous | 1.1.4 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-use-rect | 1.1.4 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-use-size | 1.1.5 | MIT | https://radix-ui.com/primitives |
| @radix-ui/react-visually-hidden | 1.2.13 | MIT | https://radix-ui.com/primitives |
| @radix-ui/rect | 1.1.3 | MIT | https://radix-ui.com/primitives |
| @standard-schema/spec | 1.1.0 | MIT | https://standardschema.dev |
| @swc/helpers | 0.5.15 | Apache-2.0 | https://swc.rs |
| @types/d3-color | 3.1.3 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/d3-color |
| @types/d3-drag | 3.0.7 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/d3-drag |
| @types/d3-interpolate | 3.0.4 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/d3-interpolate |
| @types/d3-selection | 3.0.12 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/d3-selection |
| @types/d3-transition | 3.0.9 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/d3-transition |
| @types/d3-zoom | 3.0.9 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/d3-zoom |
| @types/debug | 4.1.13 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/debug |
| @types/estree | 1.0.10 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/estree |
| @types/estree-jsx | 1.0.5 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/estree-jsx |
| @types/hast | 3.0.5 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/hast |
| @types/json-schema | 7.0.15 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/json-schema |
| @types/mdast | 4.0.4 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/mdast |
| @types/ms | 2.1.0 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/ms |
| @types/node | 22.20.5 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/node |
| @types/react | 19.3.0 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/react |
| @types/react-dom | 19.3.0 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/react-dom |
| @types/unist | 2.0.11 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/unist |
| @types/uuid | 10.0.0 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/uuid |
| @ungap/structured-clone | 1.4.0 | ISC | https://github.com/ungap/structured-clone#readme |
| @xyflow/react | 12.12.0 | MIT | https://reactflow.dev |
| @xyflow/system | 0.0.83 | MIT | https://github.com/xyflow/xyflow#readme |
| argparse | 2.0.1 | Python-2.0 | https://github.com/nodeca/argparse#readme |
| aria-hidden | 1.2.6 | MIT | https://github.com/theKashey/aria-hidden#readme |
| assistant-cloud | 0.2.11 | MIT | https://www.assistant-ui.com/ |
| assistant-stream | 0.3.57 | MIT | https://www.assistant-ui.com/ |
| bail | 2.0.2 | MIT | https://github.com/wooorm/bail#readme |
| baseline-browser-mapping | 2.11.35 | Apache-2.0 | https://github.com/web-platform-dx/baseline-browser-mapping#readme |
| browserslist | 4.29.5 | MIT | https://github.com/browserslist/browserslist#readme |
| caniuse-lite | 1.0.30001826 | CC-BY-4.0 | https://github.com/browserslist/caniuse-lite#readme |
| ccount | 2.0.1 | MIT | https://github.com/wooorm/ccount#readme |
| character-entities | 2.0.2 | MIT | https://github.com/wooorm/character-entities#readme |
| character-entities-html4 | 2.1.0 | MIT | https://github.com/wooorm/character-entities-html4#readme |
| character-entities-legacy | 3.0.0 | MIT | https://github.com/wooorm/character-entities-legacy#readme |
| character-reference-invalid | 2.0.1 | MIT | https://github.com/wooorm/character-reference-invalid#readme |
| class-variance-authority | 0.7.1 | Apache-2.0 | https://github.com/joe-bell/cva#readme |
| classcat | 5.0.5 | MIT | https://github.com/jorgebucaran/classcat#readme |
| client-only | 0.0.1 | MIT | https://reactjs.org/ |
| clsx | 2.1.1 | MIT | https://github.com/lukeed/clsx#readme |
| cmdk | 1.1.1 | MIT | https://github.com/pacocoursey/cmdk#readme |
| comma-separated-tokens | 2.0.3 | MIT | https://github.com/wooorm/comma-separated-tokens#readme |
| compare-versions | 6.1.1 | MIT | https://github.com/omichelsen/compare-versions#readme |
| convert-source-map | 2.0.0 | MIT | https://github.com/thlorenz/convert-source-map |
| csstype | 3.2.3 | MIT | https://github.com/frenic/csstype#readme |
| d3-color | 3.1.1 | ISC | https://d3js.org/d3-color/ |
| d3-dispatch | 3.0.2 | ISC | https://d3js.org/d3-dispatch/ |
| d3-drag | 3.0.1 | ISC | https://d3js.org/d3-drag/ |
| d3-ease | 3.0.2 | BSD-3-Clause | https://d3js.org/d3-ease/ |
| d3-interpolate | 3.0.2 | ISC | https://d3js.org/d3-interpolate/ |
| d3-selection | 3.0.1 | ISC | https://d3js.org/d3-selection/ |
| d3-timer | 3.0.2 | ISC | https://d3js.org/d3-timer/ |
| d3-transition | 3.0.2 | ISC | https://d3js.org/d3-transition/ |
| d3-zoom | 3.0.1 | ISC | https://d3js.org/d3-zoom/ |
| debug | 4.4.3 | MIT | https://github.com/debug-js/debug#readme |
| decode-named-character-reference | 1.3.0 | MIT | https://github.com/wooorm/decode-named-character-reference#readme |
| dequal | 2.0.3 | MIT | https://github.com/lukeed/dequal#readme |
| detect-libc | 2.1.2 | Apache-2.0 | https://github.com/lovell/detect-libc#readme |
| detect-node-es | 1.1.0 | MIT | https://github.com/thekashey/detect-node |
| devlop | 1.1.0 | MIT | https://github.com/wooorm/devlop#readme |
| electron-to-chromium | 1.5.481 | ISC | https://github.com/Kilian/electron-to-chromium#readme |
| escalade | 3.2.0 | MIT | https://github.com/lukeed/escalade#readme |
| escape-string-regexp | 5.0.0 | MIT | https://github.com/sindresorhus/escape-string-regexp#readme |
| estree-util-is-identifier-name | 3.0.0 | MIT | https://github.com/syntax-tree/estree-util-is-identifier-name#readme |
| extend | 3.0.2 | MIT | https://github.com/justmoon/node-extend#readme |
| fast-json-patch | 3.1.1 | MIT | https://github.com/Starcounter-Jack/JSON-Patch |
| gensync | 1.0.0-beta.2 | MIT | https://github.com/loganfsmyth/gensync |
| get-nonce | 1.0.1 | MIT | https://github.com/theKashey/get-nonce |
| hast-util-to-jsx-runtime | 2.3.7 | MIT | https://github.com/syntax-tree/hast-util-to-jsx-runtime#readme |
| hast-util-whitespace | 3.0.0 | MIT | https://github.com/syntax-tree/hast-util-whitespace#readme |
| html-url-attributes | 3.0.1 | MIT | https://github.com/rehypejs/rehype-minify/tree/main#readme |
| inline-style-parser | 0.2.9 | MIT | https://github.com/remarkablemark/inline-style-parser#readme |
| is-alphabetical | 2.0.1 | MIT | https://github.com/wooorm/is-alphabetical#readme |
| is-alphanumerical | 2.0.1 | MIT | https://github.com/wooorm/is-alphanumerical#readme |
| is-decimal | 2.0.1 | MIT | https://github.com/wooorm/is-decimal#readme |
| is-hexadecimal | 2.0.1 | MIT | https://github.com/wooorm/is-hexadecimal#readme |
| is-plain-obj | 4.1.0 | MIT | https://github.com/sindresorhus/is-plain-obj#readme |
| js-tokens | 4.0.0 | MIT | https://github.com/lydell/js-tokens#readme |
| js-yaml | 4.3.2 | MIT | https://github.com/nodeca/js-yaml#readme |
| jsesc | 3.1.0 | MIT | https://mths.be/jsesc |
| json5 | 2.2.3 | MIT | http://json5.org/ |
| longest-streak | 3.1.0 | MIT | https://github.com/wooorm/longest-streak#readme |
| lru-cache | 5.1.1 | ISC | https://github.com/isaacs/node-lru-cache#readme |
| lucide-react | 1.65.0 | ISC | https://lucide.dev |
| markdown-table | 3.1.0 | MIT | https://github.com/wooorm/markdown-table#readme |
| mdast-util-find-and-replace | 3.0.4 | MIT | https://github.com/syntax-tree/mdast-util-find-and-replace#readme |
| mdast-util-from-markdown | 2.1.0 | MIT | https://github.com/syntax-tree/mdast-util-from-markdown#readme |
| mdast-util-gfm | 3.1.0 | MIT | https://github.com/syntax-tree/mdast-util-gfm#readme |
| mdast-util-gfm-autolink-literal | 2.1.0 | MIT | https://github.com/syntax-tree/mdast-util-gfm-autolink-literal#readme |
| mdast-util-gfm-footnote | 2.1.0 | MIT | https://github.com/syntax-tree/mdast-util-gfm-footnote#readme |
| mdast-util-gfm-strikethrough | 2.0.1 | MIT | https://github.com/syntax-tree/mdast-util-gfm-strikethrough#readme |
| mdast-util-gfm-table | 2.0.1 | MIT | https://github.com/syntax-tree/mdast-util-gfm-table#readme |
| mdast-util-gfm-task-list-item | 2.0.1 | MIT | https://github.com/syntax-tree/mdast-util-gfm-task-list-item#readme |
| mdast-util-mdx-expression | 2.0.1 | MIT | https://github.com/syntax-tree/mdast-util-mdx-expression#readme |
| mdast-util-mdx-jsx | 3.2.1 | MIT | https://github.com/syntax-tree/mdast-util-mdx-jsx#readme |
| mdast-util-mdxjs-esm | 2.0.1 | MIT | https://github.com/syntax-tree/mdast-util-mdxjs-esm#readme |
| mdast-util-phrasing | 4.1.0 | MIT | https://github.com/syntax-tree/mdast-util-phrasing#readme |
| mdast-util-to-hast | 13.2.1 | MIT | https://github.com/syntax-tree/mdast-util-to-hast#readme |
| mdast-util-to-markdown | 2.2.3 | MIT | https://github.com/syntax-tree/mdast-util-to-markdown#readme |
| mdast-util-to-string | 4.0.0 | MIT | https://github.com/syntax-tree/mdast-util-to-string#readme |
| micromark | 4.0.3 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-core-commonmark | 2.0.4 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-extension-gfm | 3.0.0 | MIT | https://github.com/micromark/micromark-extension-gfm#readme |
| micromark-extension-gfm-autolink-literal | 2.1.0 | MIT | https://github.com/micromark/micromark-extension-gfm-autolink-literal#readme |
| micromark-extension-gfm-footnote | 2.1.0 | MIT | https://github.com/micromark/micromark-extension-gfm-footnote#readme |
| micromark-extension-gfm-strikethrough | 2.1.0 | MIT | https://github.com/micromark/micromark-extension-gfm-strikethrough#readme |
| micromark-extension-gfm-table | 2.1.2 | MIT | https://github.com/micromark/micromark-extension-gfm-table#readme |
| micromark-extension-gfm-tagfilter | 2.0.0 | MIT | https://github.com/micromark/micromark-extension-gfm-tagfilter#readme |
| micromark-extension-gfm-task-list-item | 2.1.0 | MIT | https://github.com/micromark/micromark-extension-gfm-task-list-item#readme |
| micromark-factory-destination | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-factory-label | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-factory-space | 2.1.0 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-factory-title | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-factory-whitespace | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-character | 2.1.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-chunked | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-classify-character | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-combine-extensions | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-decode-numeric-character-reference | 2.0.2 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-decode-string | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-edit-map | 1.0.0 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-encode | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-html-tag-name | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-normalize-identifier | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-resolve-all | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-sanitize-uri | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-subtokenize | 2.1.0 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-symbol | 2.0.1 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| micromark-util-types | 2.0.3 | MIT | https://github.com/micromark/micromark/tree/main#readme |
| ms | 2.1.3 | MIT | https://github.com/vercel/ms#readme |
| nanoid | 3.3.20 | MIT | https://github.com/ai/nanoid#readme |
| next | 15.5.29 | MIT | https://nextjs.org |
| node-releases | 2.0.63 | MIT | https://github.com/chicoxyzzy/node-releases#readme |
| parse-entities | 4.0.2 | MIT | https://github.com/wooorm/parse-entities#readme |
| picocolors | 1.1.1 | ISC | https://github.com/alexeyraspopov/picocolors#readme |
| postcss | 8.4.31 | MIT | https://postcss.org/ |
| property-information | 7.2.0 | MIT | https://github.com/wooorm/property-information#readme |
| radix-ui | 1.8.1 | MIT | https://radix-ui.com/primitives |
| react | 19.3.1 | MIT | https://react.dev/ |
| react-dom | 19.3.1 | MIT | https://react.dev/ |
| react-markdown | 10.1.0 | MIT | https://github.com/remarkjs/react-markdown#readme |
| react-remove-scroll | 2.7.2 | MIT | https://github.com/theKashey/react-remove-scroll#readme |
| react-remove-scroll-bar | 2.3.8 | MIT | https://github.com/theKashey/react-remove-scroll-bar#readme |
| react-resizable-panels | 4.15.3 | MIT | https://react-resizable-panels.vercel.app/ |
| react-style-singleton | 2.2.3 | MIT | https://github.com/theKashey/react-style-singleton#readme |
| react-textarea-autosize | 8.5.9 | MIT | https://github.com/Andarist/react-textarea-autosize#readme |
| remark-gfm | 4.0.1 | MIT | https://github.com/remarkjs/remark-gfm#readme |
| remark-parse | 11.0.0 | MIT | https://remark.js.org |
| remark-rehype | 11.1.2 | MIT | https://github.com/remarkjs/remark-rehype#readme |
| remark-stringify | 11.0.0 | MIT | https://remark.js.org |
| rxjs | 7.8.1 | Apache-2.0 | https://rxjs.dev |
| safe-content-frame | 0.0.34 | MIT | https://www.assistant-ui.com/safe-content-frame |
| scheduler | 0.28.0 | MIT | https://react.dev/ |
| secure-json-parse | 4.1.1 | BSD-3-Clause | https://github.com/fastify/secure-json-parse#readme |
| semver | 6.3.2 | ISC | https://github.com/npm/node-semver#readme |
| sharp | 0.35.5 | Apache-2.0 | https://sharp.pixelplumbing.com |
| sonner | 2.0.8 | MIT | https://sonner.emilkowal.ski/ |
| source-map-js | 1.2.2 | BSD-3-Clause | https://github.com/7rulnik/source-map-js |
| space-separated-tokens | 2.0.2 | MIT | https://github.com/wooorm/space-separated-tokens#readme |
| stringify-entities | 4.0.4 | MIT | https://github.com/wooorm/stringify-entities#readme |
| style-to-object | 2.0.2 | MIT | https://github.com/remarkablemark/style-to-object#readme |
| styled-jsx | 5.1.6 | MIT | https://github.com/vercel/styled-jsx#readme |
| tailwind-merge | 3.7.0 | MIT | https://github.com/dcastil/tailwind-merge |
| trim-lines | 3.0.1 | MIT | https://github.com/wooorm/trim-lines#readme |
| trough | 2.2.0 | MIT | https://github.com/wooorm/trough#readme |
| tslib | 2.9.1 | 0BSD | https://www.typescriptlang.org/ |
| undici-types | 6.21.0 | MIT | https://undici.nodejs.org |
| unified | 11.0.5 | MIT | https://unifiedjs.com |
| unist-util-is | 6.0.1 | MIT | https://github.com/syntax-tree/unist-util-is#readme |
| unist-util-position | 5.0.0 | MIT | https://github.com/syntax-tree/unist-util-position#readme |
| unist-util-stringify-position | 4.0.0 | MIT | https://github.com/syntax-tree/unist-util-stringify-position#readme |
| unist-util-visit | 5.2.0 | MIT | https://github.com/syntax-tree/unist-util-visit#readme |
| unist-util-visit-parents | 6.1.0 | MIT | https://github.com/syntax-tree/unist-util-visit-parents#readme |
| untruncate-json | 0.0.1 | MIT | https://github.com/dphilipson/untruncate-json |
| update-browserslist-db | 1.3.4 | MIT | https://github.com/browserslist/update-db#readme |
| use-callback-ref | 1.3.3 | MIT | https://github.com/theKashey/use-callback-ref#readme |
| use-composed-ref | 1.4.0 | MIT | https://github.com/Andarist/use-composed-ref#readme |
| use-isomorphic-layout-effect | 1.2.1 | MIT | https://github.com/Andarist/use-isomorphic-layout-effect#readme |
| use-latest | 1.3.0 | MIT | https://github.com/Andarist/use-latest#readme |
| use-sidecar | 1.1.3 | MIT | https://github.com/theKashey/use-sidecar |
| use-sync-external-store | 1.7.0 | MIT | https://github.com/react/react#readme |
| uuid | 11.1.1 | MIT | https://github.com/uuidjs/uuid#readme |
| vfile | 6.0.3 | MIT | https://github.com/vfile/vfile#readme |
| vfile-message | 4.0.4 | MIT | https://github.com/vfile/vfile-message#readme |
| yallist | 3.1.1 | ISC | https://github.com/isaacs/yallist#readme |
| zod | 3.25.76 | MIT | https://zod.dev |
| zustand | 4.5.7 | MIT | https://github.com/pmndrs/zustand |
| zwitch | 2.0.4 | MIT | https://github.com/wooorm/zwitch#readme |

## Notes on non-permissive entries

- **@bufbuild/protobuf** ((Apache-2.0 AND BSD-3-Clause)): Dual Apache-2.0/BSD-3-Clause; the Apache-2.0 component is compatible with this project's license.
- **@fontsource-variable/inter** (OFL-1.1): OFL-1.1 font files, not source code; the OFL permits bundling in applications.
- **@fontsource-variable/jetbrains-mono** (OFL-1.1): OFL-1.1 font files, not source code; the OFL permits bundling in applications.
- **@img/sharp-libvips-darwin-arm64** (LGPL-3.0-or-later): LGPL-3.0-or-later build-time image processing library used unmodified; this is a native binary dependency bundled at build time.
- **caniuse-lite** (CC-BY-4.0): CC-BY-4.0 data file providing browser support information; this table serves as proper attribution.
- **email-validator** (The Unlicense (Unlicense)): Uses the Unlicense, a permissive public domain dedication that is compatible with Apache-2.0.
- **psycopg** (LGPL-3.0-only): LGPL-3.0-only library used as a dynamically linked dependency; the LGPL permits this use with Apache-2.0 code provided the library is not modified and users can relink.
- **psycopg-binary** (LGPL-3.0-only): LGPL-3.0-only library used as a dynamically linked dependency; the LGPL permits this use with Apache-2.0 code provided the library is not modified and users can relink.

