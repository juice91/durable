# Durable execution

A run is executed as a sequence of recorded steps. When a worker process dies, because of a crash, a deploy or a machine
restart, a worker with the same executor id picks the run up again and continues from the record.

## What recovery does

| Where the process died | What recovery does |
| --- | --- |
| After a step was recorded | The step is **not** run again. Its recorded result is used. |
| While a model was answering | The model request is made again. The half-streamed text of the first attempt is dropped by the interface. |
| While a read-only tool was running | The tool is called again. |
| After a tool that changes something may have reached the outside world, and before its result was recorded | See below: the platform does not blindly repeat a call whose outcome it cannot know. |
| While a run waited for a person | The run is still waiting. The wait is a row in Postgres, not a process in memory. |

## Calls with side effects: reserve, call, complete

Before a call that changes something, the worker **reserves** a key for it in the database. It then makes the call and
**completes** the reservation with the result.

- A recovered step that finds a completed reservation uses the recorded result.
- A recovered step that finds a reservation without a result cannot know whether the call arrived. For `http.request` and
  for calls to MCP tools, the run ends in the status `needs_attention` and the call is **not** sent again. A person checks
  the other system and decides.
- A tool whose external service deduplicates by an idempotency key may be called again with the same key, and the
  service answers with the first result.

This is the honest limit of any system that talks to services it does not control: exactly-once delivery needs the other
side's help. Where that help is missing, Durable stops and says so.

## Determinism

A recovered run must take the same path as the first attempt. Anything that could differ on a second look (the clock,
the "latest" version of an agent, a deadline) is read once inside a step and recorded.

## See it yourself

Start a workflow with a few slow steps, restart the worker while it runs, and open the run in Studio: the header shows a
**Recovered** badge, and the completed steps were not run again. The walkthrough is in
[Proving durability](../workflows/proving-durability.md).

With the Docker Compose quick start, restart the worker with:

```sh
docker compose restart worker
```

The repository's crash-injection suite kills real API and worker processes at named points and checks these rules; see
[the suite's description](../../tests/recovery/README.md).
