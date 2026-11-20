# Cloudflare Workers AI

The `cloudflare` provider runs Workers AI models through Cloudflare's OpenAI-compatible API, optionally routed through
**AI Gateway**. Model ids keep the `@cf/` prefix: `cloudflare:@cf/deepseek-ai/deepseek-v4.1-flash-1017`.

## Configure

| Variable | Where | Meaning |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` (fallback `EAP_CLOUDFLARE_API_TOKEN`) | Secret | API token. Required. |
| `EAP_CLOUDFLARE_ACCOUNT_ID` | ConfigMap | Account id. Required. |
| `EAP_CLOUDFLARE_GATEWAY_ID` | ConfigMap | AI Gateway id. Optional: sent as the `cf-aig-gateway-id` header on every request. |
| `EAP_CLOUDFLARE_BASE_URL` | ConfigMap | Optional override of the OpenAI-compatible base. Default `https://api.cloudflare.com/client/v4/accounts/<account id>/ai/v1`. |

Also allow the provider: add `cloudflare:*` (or specific models) to `EAP_ALLOWED_MODELS`. Without a token or account id the
provider shows **Not configured** in Settings → Providers; a failed model listing shows **Unreachable**.

- **API token**: Cloudflare dashboard → *My Profile* → *API Tokens* → *Create Token*, use the *Workers AI* template (or a custom
  token with *Workers AI: Read* and *Workers AI: Edit* (run) on the account). Workers AI needs a paid Workers plan.
- **Account id**: dashboard → *Workers & Pages* → the *Account ID* on the right, or the id in the dashboard URL.
- **Gateway id**: dashboard → *AI* → *AI Gateway* → create a gateway; its name is the id. AI Gateway adds request logging,
  caching, rate limiting and usage analytics in front of Workers AI. Leave the variable empty to call Workers AI directly.

## Models

The list comes from the account's text-generation catalogue (`GET /accounts/<id>/ai/models/search?task=Text Generation`,
up to five pages of 100). Function calling and context size are read from each model's catalogue properties.
`@cf/deepseek-ai/deepseek-v4.1-flash-1017` is known statically: 1,048,576-token context, tool calling, JSON `response_format`,
`reasoning_effort` none, low, high or max.

Models are treated as text-only (no image or PDF input) unless the catalogue marks them as vision models. Embeddings are not
served by this provider; use the `litellm` embedding models.

## Pricing

Listed in `config/model-prices.json`, USD per million tokens: `@cf/deepseek-ai/deepseek-v4.1-flash-1017` input 0.30, output
1.20, cached input 0.006. Other Workers AI models are unpriced until added there.

## Architect

When configured and allowed, the Architect may pick `cloudflare:` models. They rank behind Anthropic and OpenAI and never become
the built-in agents' default on their own.
