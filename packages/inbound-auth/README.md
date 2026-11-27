# eap-inbound

Pure verification of inbound webhook signatures (design: an internal design note,
section 4.3). Standard library and pydantic only: no I/O, no web, no database.

`verify(preset, config, headers, raw_body, secrets, now, tolerance)` returns a `Verified`; the service decides
everything else. Presets: `github`, `standard_webhooks`, `custom`. `dedupe_key(...)` builds the delivery's dedupe key
from signed material only. `eap_inbound.signing` signs a body the way a sender does (tests, docs, sample senders).
