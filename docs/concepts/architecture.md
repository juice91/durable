# How Durable works

Durable is three processes and one database.

| Part | What it does |
| --- | --- |
| **Studio** | The web console (Next.js). It talks only to the API. |
| **API** | The control plane (FastAPI). It validates and stores definitions, authorises callers, records runs and puts them on a queue. It never runs an agent itself. |
| **Worker** | Executes agents, tool calls, workflows, schedules and document ingest as durable workflows (DBOS and Pydantic AI). |
| **Postgres** | Everything that must survive: definitions, runs, events, queues, workflow checkpoints and, through pgvector, embeddings. |

The API and the worker never call each other. They meet in Postgres: the API writes the run and enqueues it, the worker
picks it up and writes events, and the API streams those events to whoever asks.

## Definitions are versioned

You edit an agent or a workflow as a **draft**. **Publishing** makes an immutable version. A run records the version it
runs, so changing a draft never changes a run, and publishing a new version does not change a run that already started.
Earlier versions can be restored, and agent versions can be compared.

A workflow is data, not code: a JSON document with typed steps, checked before it can be published. Branch conditions
are expressions over recorded values, so a published workflow cannot run arbitrary programs.

## What a run is

A **run** is one execution of an agent, a workflow, a tool test, a knowledge ingest or an evaluation.

- The run id is the durable workflow id. Starting a run with the same id and the same input again returns the same run.
- A run has a **gap-free event log**. Every event has a sequence number, so a client that lost its connection resumes the
  stream from the last number it saw.
- Model requests record token usage and cost.
- Each run has one OpenTelemetry trace.

## Tenants

Every row and every query is scoped to a tenant. An id that belongs to another tenant answers `404`, the same as an id
that does not exist. API keys carry their tenant; a request cannot name one.

## Plug-ins

Identity, authorisation and membership are plug-ins ([Identity and access](../iam.md)), and so are knowledge-source
providers. The core ships interfaces; the implementations are separate packages found through Python entry points.
