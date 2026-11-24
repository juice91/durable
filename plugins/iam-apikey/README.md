# eap-iam-apikey

Identity plug-in (`eap.iam.identity`, name `apikey`): authenticates tenant API keys `pos_<id>_<secret>` and the
Architect's run token behind the `IdentityProvider` interface, with the behaviour the API always had: scopes, agent and
workflow allowlists, allowed origins, throttled `last_used_at`, one answer for every kind of invalid key.

The `api_keys` table and its store stay in `eap-run-store` (migration `0017_api_keys` is history); this plug-in reads them.
