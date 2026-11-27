# Durable documentation

Durable is a self-hosted platform for building, running and observing AI agents and workflows. A FastAPI control plane
records every run, a worker executes it step by step as a durable workflow in Postgres, and Studio is the web console.

## Start here

- [Quick start with Docker Compose](quickstart-compose.md): the whole platform on one machine.
- [How Durable works](concepts/architecture.md): the processes, the database and what a run is.
- [Durable execution](concepts/durable-execution.md): what happens when a process dies mid-run.

## Build

| You want to | Read |
| --- | --- |
| Give a model instructions, tools, knowledge and memory | [Agents](guides/agents.md) |
| Fix the order of steps and let models fill some of them | [Workflows](guides/workflows.md) |
| Run a workflow on a cron expression | [Schedules](guides/schedules.md) |
| Stop for a person before something happens | [Approvals](guides/approvals.md) |
| Start a run from another system | [Inbound webhooks](webhooks.md) |
| Answer from your own documents | [Knowledge bases](guides/knowledge-bases.md) |
| Add tools from Model Context Protocol servers | [MCP servers](mcp-servers.md) |
| Let an outside agent call your workflows | [External agents](external-access.md) |

## Operate

| Topic | Read |
| --- | --- |
| Sign-in, roles and API keys | [Identity and access](iam.md) |
| Model providers | [Model providers](guides/model-providers.md) |
| Traces, logs and metrics | [Observability](guides/observability.md) |
| Publishing Studio and the API | [Expose Studio and the API](expose.md) |
| Settings | [Configuration](configuration.md) |

The HTTP API reference is generated from the API's OpenAPI schema when this site is built.
