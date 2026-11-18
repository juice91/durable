# eap-kb-sources

The provider-neutral side of knowledge-base source sync: the `SourceAdapter` protocol
(`adapter.py`), error codes (`errors.py`), the token bucket and the daily quota kept in the source
checkpoint (`budget.py`), an adapter registry (`registry.py`) and a deterministic `FakeCatalogAdapter`
for tests (`fake.py`). Provider adapters live under `providers/<name>/` and register themselves; the
worker (`eap_worker.kb_source_sync`) owns the DBOS workflow and drives whatever adapter the source's
provider names. This package never imports `eap_api`, `eap_worker`, `fastapi` or `dbos` (lint-imports).
