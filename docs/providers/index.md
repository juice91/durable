# Provider packages

A knowledge-base source provider is a plug-in: the GitHub adapter is built into `eap-kb-sources` (see `github.md`); every other
provider is its own distribution that declares an entry point in the group `eap.kb_sources.adapters` and is found at start-up.
Install a provider by adding its distribution to the environment of the API and the worker (a uv workspace member of your deployment repository, or a pip-installed wheel), then select
its `provider` value in a connection and a source. Each provider documents its hosts, secrets and quotas in its own README. To
write one, see "Writing a provider plug-in" in `docs/kb-sources.md`.

## Model providers

LLM providers are configured on the server, not in a plug-in: see `anthropic-models.md` and `cloudflare.md` (Workers AI,
through Cloudflare AI Gateway).
