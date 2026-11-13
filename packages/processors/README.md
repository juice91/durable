# eap-processors

Pure, deterministic implementations of the built-in agent processors declared in
`eap_schemas.processors`: `pii_redact`, `regex_blocklist` and `max_length`. Every regex runs on RE2
(`google-re2`), so matching is linear in the input and patterns cannot cause ReDoS. Outcomes carry
the action taken and match counts, never the matched text. `moderation` calls a model and runs in
the worker. Imports only the standard library, `eap_schemas` and `re2`.
