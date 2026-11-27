# Model providers

Durable calls models you configure on the server. Keys are read from the environment of the API and the worker; they are
never stored in a definition.

| Provider | Settings | Model ids |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY`, optionally `OPENAI_BASE_URL` | `openai:<model>` |
| Anthropic | `ANTHROPIC_API_KEY` | `anthropic:<model>` ([model discovery](../providers/anthropic-models.md)) |
| An OpenAI-compatible gateway (LiteLLM and others) | `EAP_LITELLM_BASE_URL`, `EAP_LITELLM_API_KEY` | `litellm:<model>` |
| Cloudflare Workers AI | see [Cloudflare Workers AI](../providers/cloudflare.md) | `cloudflare:<model>` |
| Scripted test models | none | `test:<name>`: no network, no cost, used by the test suites |

With the Compose quick start, put the settings in `.env` and run `docker compose up -d` again. Studio shows each
provider's state under **Settings → Providers**.

## The allowlist

`EAP_ALLOWED_MODELS` lists the models people may use, as `provider:name` or `provider:*`. It is checked when an agent is
published, when a run starts and before every call. The Compose file allows all models of OpenAI, Anthropic and the
gateway; elsewhere, when it is unset, only `test:*` models are allowed.

## Embeddings

Knowledge bases and semantic recall have their own allowlist, `EAP_ALLOWED_EMBEDDING_MODELS`, written as
`provider:name=<dimensions>` with 768, 1024 or 1536 dimensions. `openai:*` embedding models use
`EAP_OPENAI_EMBEDDING_API_KEY`; a gateway can have its own `EAP_LITELLM_EMBEDDING_BASE_URL` and key.

## Cost

Each model request records its token usage and a cost taken from the price table (`config/model-prices.json`, which
`EAP_MODEL_PRICES_FILE` replaces). A model with no price is recorded without a cost, and a cost cap cannot act on it.
