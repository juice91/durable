# Model prices

`model-prices.json` is the operator price override the worker reads when it records a model
request (`EAP_MODEL_PRICES_FILE` points elsewhere). Keys are our model ids, `provider:name` or
`provider:*` (an exact key wins); each holds price periods in USD per million tokens, and the period
with the latest `effective_from` on or before the request date applies. A missing cache price
falls back to the input price.

Lookup order: this file, then `genai-prices` for direct providers only. `test:*` and `litellm:*`
(self-hosted models behind the LiteLLM proxy) are priced only from this file. A model with no price
is recorded with `cost_usd` null ("no price"). Cost is computed once, when the request is recorded,
and never recomputed, so editing this file changes the cost of future requests only. An invalid file
prices nothing (and the worker logs an error) until it is fixed.

`model-prices.test.json` prices `test:*` at fixed non-zero values so that tests can check the
cost arithmetic (the worker tests point `EAP_MODEL_PRICES_FILE` at it). The embedders
`test:hash` and `test:hash768` have their own price (USD 0.13 per million input tokens) for the
embedding cost checks. Embedding calls are priced by the same lookup, with output tokens 0, and
recorded with `operation = embedding`; `litellm:*` embedding models are 0 in the repo default.
