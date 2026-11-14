# eap-pricing

The operator model price override file (`EAP_MODEL_PRICES_FILE`, default `config/model-prices.json`)
and the cost arithmetic on it, shared by the worker (which records each request's cost) and the API
(which estimates a reindex's embedding cost). Standard library and pydantic only: genai-prices
lookups stay in the worker (`eap_worker.pricing`), which the API may not consult.
