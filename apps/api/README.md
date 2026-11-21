# eap-api

Control-plane API for the Enterprise Agent Platform.

Upgrading (migrations, deploy order, queue draining, `EAP_APP_VERSION`): see the "Upgrading"
section of `apps/worker/README.md`.

Code layout and layering rules (controllers, services, data access, dependency providers): see
`docs/api-layering.md`.

## Settings (RAG and memory)

All optional; the commented defaults are in `.dev.env.example`. The API parses the knowledge
settings at start (`routes.knowledge.check_knowledge_configuration`), so a bad value stops it
instead of failing a request.

| Setting | Default | What it does |
|---|---|---|
| `EAP_ALLOWED_EMBEDDING_MODELS` | `test:*` | Embedding model allowlist (`provider:name=dimensions`); a knowledge base, a reindex and an agent's `memory.semantic_recall` may only name an allowed model, and the dimensions come from here. Must match the worker's |
| `EAP_EMBEDDING_QUERY_INSTRUCTIONS` | none | JSON `{model_id: instruction}`; parsed with the allowlist (the worker applies it) |
| `EAP_KB_MAX_DOCUMENT_BYTES` | 5242880 (5 MiB) | Body cap of the document upload route (413 above it) |
| `EAP_KB_MAX_INGEST_TOKENS` | 2000000 | The per-run embedding budget the worker enforces; the API's reindex dry run (`dry_run: true`) reports whether the estimate exceeds it. Keep it equal to the worker's |
| `EAP_KB_EXACT_SEARCH_MAX_CHUNKS` | 20000 | Search tester and retrieve: when the HNSW index returns fewer than `top_k` hits and the searched knowledge bases hold at most this many active chunks, the search runs again exactly; 0 turns the fallback off |
| `EAP_MODEL_PRICES_FILE` | `config/model-prices.json` | Prices for the reindex dry run's cost estimate (the file the worker prices usage with) |
| `EAP_AUDIT_HASH_SALT` | unset (plain SHA-256) | Key of the HMAC that the forget-resource audit log uses for the resource id; set it per deployment so the hash cannot be looked up offline |

Retention (`EAP_MEMORY_RETENTION_*`) and the embedding gateway settings are worker-only: see
`apps/worker/README.md`.
