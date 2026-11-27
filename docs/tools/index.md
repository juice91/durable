# Built-in tools

Agents and workflow steps call tools by id. `GET /api/v1/tools` lists the tools of your installation with their input and
output schemas; `EAP_ALLOWED_TOOLS` limits which may be used. Tools from other systems come in through
[MCP servers](../mcp-servers.md).

| Tool | What it does |
| --- | --- |
| [`http.request`](http-request.md) | Calls an HTTP API under the outbound policy, with stored secrets by reference. |
| [`feed.parse`](feed-parse.md) | Reads an RSS or Atom document. |
| [`html.extract`](html-extract.md) | Extracts text and fields from HTML. |
| [`json.query`](json-query.md) | Selects and shapes JSON. |
| [`text.check`](text-check.md) | Checks text against length and content rules. |
| [`atproto.richtext`](atproto-richtext.md) | Builds rich text for AT Protocol posts. |
| [`workflow.history`](workflow-history.md) | Reads results of the workflow's own earlier runs. |
| [`clock.now`](clock.md) | The current time. |
| `clock.sleep` | Waits 1 to 120 seconds, for pacing and demonstrations. |
| `workflow.return` | Ends a workflow run with a result. |
| `secrets.request` | Asks a person for a secret on an approval card with a masked input. |
| `tickets.create` | A demonstration ticket tool used by the examples and tests. |

A tool that changes something outside the platform reserves its call before making it
([Durable execution](../concepts/durable-execution.md)).
