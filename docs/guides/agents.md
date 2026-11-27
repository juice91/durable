# Agents

An agent is a model plus instructions, tools, knowledge and memory, saved as a versioned specification. Use an agent
when the model should decide what to do next. When the steps are known in advance, use a [workflow](workflows.md) that
calls the agent in one of them.

## The specification

| Part | Field | What it sets |
| --- | --- | --- |
| Name and model | `name`, `model`, `model_settings` | Which model answers, as `provider` and `name` |
| Instructions | `instructions` | The system prompt |
| Tools | `tools`, `mcp_tools` | [Built-in tools](../tools/index.md) and tools of registered [MCP servers](../mcp-servers.md) |
| Other agents | `agent_tools` | Published agents the model may call ([agents as tools](../agents/agent-tools.md)) |
| Approvals | `approvals` | Tools that need a person's decision before each call ([Approvals](approvals.md)) |
| Request context | `context_fields` | Typed values a caller supplies with each run |
| Processors | `input_processors`, `output_processors`, `step_processors`, `stream_processors` | Guardrails such as `pii_redact`, `regex_blocklist`, `max_length` and `moderation` |
| Knowledge | `knowledge_bases` | Up to 8 [knowledge bases](knowledge-bases.md) the agent can search |
| Memory | `memory` | Working memory, observational memory and semantic recall |
| Output | `output` | A JSON Schema the answer must follow |
| Limits | `limits`, `fallback_models` | Caps per run (requests, tool calls, tokens, cost) and up to 3 fallback models |

## In Studio

Open **Agents**, then **New agent**, or describe what you want in the [Architect](../architect.md) and review the draft
it writes. The editor walks through the parts above. **Test draft** opens a chat on the unpublished draft; **Publish**
creates a version.

## Through the API

Set `API` to your installation (the Compose quick start serves the API at `http://localhost:3000`) and `TOKEN` to a
credential it accepts ([Identity and access](../iam.md)).

```sh
# 1. Create a draft. Only name and model are required.
curl -X POST "$API/api/v1/agents" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{
  "spec": {
    "name": "Support triage",
    "instructions": "Classify the message as billing, bug or other, and say why in one sentence.",
    "model": {"provider": "openai", "name": "<a model your key can use>"}
  }
}'

# 2. Publish the draft as a version (AGENT_ID is the id from the answer).
curl -X POST "$API/api/v1/agents/$AGENT_ID/versions" -H "Authorization: Bearer $TOKEN"

# 3. Start a run. You choose run_id; sending the same body again returns the same run.
RUN_ID=$(uuidgen | tr 'A-Z' 'a-z')
curl -X POST "$API/api/v1/agents/$AGENT_ID/runs" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"run_id\": \"$RUN_ID\", \"input\": \"I was charged twice\", \"version\": null}"

# 4. Stream its events (server-sent events).
curl -N "$API/api/v1/runs/$RUN_ID/stream" -H "Authorization: Bearer $TOKEN"
```

A model must be in `EAP_ALLOWED_MODELS` before an agent that uses it can be published
([Model providers](model-providers.md)). Conversations (multi-turn chat) start at
`POST /api/v1/agents/{agent_id}/conversations`.

## Memory

- **Working memory** is a small note, free text or a JSON schema, that the agent reads and updates.
- **Observational memory** condenses long conversations into notes.
- **Semantic recall** looks up related earlier messages by meaning; it needs an embedding model.

Memory can be read, edited, expired by retention settings and forgotten for one user through the API.

## Check quality

Test suites run cases against a published version with scorers: deterministic rules such as
[`regex`](../evals.md), or a model as judge. Experiments compare versions.
