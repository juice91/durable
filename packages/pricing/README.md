# eap-pricing

The model price lookup shared by the worker (which records each request's cost) and the API (which
estimates a reindex's embedding cost, and asks `price_known` whether a model has a price):

- the operator override file (`EAP_MODEL_PRICES_FILE`, default `config/model-prices.json`) and the
  cost arithmetic on it;
- the genai-prices fallback (bundled snapshot, no network), never for `test:*` or `litellm:*`;
- `price_request(model_id, usage, at)`, the worker's one lookup, and
  `price_known(model_id, day) -> "priced" | "zero" | "unpriced"`, which uses the same lookup, so the
  two can never disagree.

Standard library, pydantic and genai-prices only (import-linter contract in the root pyproject).
