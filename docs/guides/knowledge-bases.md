# Knowledge bases

A knowledge base stores documents as searchable chunks. Agents search it with the `retrieve` tool and cite what they
found. Extraction, chunking, embedding and indexing run as a durable workflow, which resumes from the last committed
batch after a restart.

Knowledge bases need an embedding model: set one up in [Model providers](model-providers.md) and allow it with
`EAP_ALLOWED_EMBEDDING_MODELS`.

## Add documents

In Studio, open **Knowledge**, create a knowledge base and upload files or paste text. Supported formats: Markdown, PDF,
reStructuredText, YAML, JSON, HTML and plain text. Through the API:

```sh
curl -X POST "$API/api/v1/knowledge-bases" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "Product docs", "embedding_model": "openai:text-embedding-3-small", "description": "Manuals and FAQs"}'

curl -X PUT "$API/api/v1/knowledge-bases/$KB_ID/documents/faq" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mime_type": "text/markdown", "title": "FAQ", "content": "# FAQ\n\nTo reset your password, ...",
       "source_url": "https://docs.example.com/faq"}'

curl -X POST "$API/api/v1/knowledge-bases/$KB_ID/search" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"query": "How do I reset my password?", "top_k": 5}'
```

PDF content goes in `content_base64`. To upload a whole directory, use the
[import script](../knowledge-import.md).

## Attach it to an agent

Add the knowledge base in the agent editor's knowledge step. Each reference has `top_k` (1 to 20, default 8) and may set
a minimum score and a metadata filter. An agent can use up to 8 knowledge bases.

## Keep it in step with a source

A **source** binds a connection to a knowledge base and is synced by a workflow that you can schedule. The GitHub
adapter is built in ([GitHub source](../providers/github.md)); other providers are plug-ins
([provider packages](../providers/index.md)).

## Change the embedding model

**Reindex** re-embeds the whole knowledge base. Run the dry run first: it estimates tokens and cost.

## Limits

- A text upload is capped at `EAP_KB_MAX_DOCUMENT_BYTES` (5 MiB by default).
- Each ingest run has a token budget, `EAP_KB_MAX_INGEST_TOKENS`.
