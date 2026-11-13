# Crash-injection matrix (milestone 1, task 9)

Kills real processes at named points and checks that a fresh process recovers the run without
duplicating the external side effect. Binding spec: `docs/plans/2026-11-11-milestone-1-vertical-slice.md`
("Durability contract", "Run hand-off contract", "Task 9 crash-injection matrix").

## Run it

```bash
bash scripts/dev-db.sh up                    # dev Postgres (eap_app + eap_dbos)
timeout 600 uv run pytest tests/recovery -v  # ~25 s; also part of scripts/test.sh
```

Without a reachable Postgres every test is skipped with the reason. Override the databases with
`EAP_TEST_DATABASE_URL` (eap_app) and `EAP_TEST_DBOS_URL` (eap_dbos). Each test has a 240 s hard
timeout. A summary of what each scenario observed is printed at the end of the run.

Each test gets its own DBOS system schema, application version, executor id and tenant, so its
workers recover and dequeue only its own run. Worker processes are stopped and the schema and the
tenant's rows (runs, events, side effects, fake tickets, agents) are deleted afterwards.

## How a crash is made

- Worker: `python -m eap_worker` is started with `EAP_CRASH_AT=<point>` (and `EAP_CRASH_EVENT_KEY`
  for scenario 3). When it reaches that point, `apps/worker/src/eap_worker/crash.py` calls
  `os._exit(137)`: no cleanup and no checkpoint, the same as `kill -9`. `EAP_CRASH_MARKER` makes
  sure it can crash only once. A second worker with the **same executor id** and no crash settings
  then recovers the pending workflow on launch.
- API: `crashing_api.py` runs the real API app in its own process and sends one real POST.
  `DBOSRunLauncher.launch`, the call right after the route commits the `runs` row, is replaced with
  `os._exit(137)`. The test then retries the same POST against a fresh API instance.

The agent is the `test:ticket` scripted model. Request 0 calls `tickets.create`, which hits the fake
external ticket service (it honours an idempotency key and counts every call). Request 1 streams
the answer text. A healthy run stores six events:
`start, model:0:final, tool:{id}:call, tool:{id}:result, model:1:final, end`.

## What every scenario asserts

1. Exactly one external ticket for the run. The number of calls the service received is reported,
   and it may legitimately be 2.
2. `seq` is exactly `0..5`, no `event_key` repeats, the event story is the healthy one above, the
   run is `completed`, and DBOS has exactly one workflow for the `run_id`, in `SUCCESS`.
3. SSE replay: for resume points -1, 0, the last seq stored before the crash, the midpoint, last-1
   and last, both `?after_seq=` and `Last-Event-ID` return exactly the database's events after that
   seq, with `id == seq`, ending on `run_completed`.

## What each scenario proves

| # | Crash point | What it proves |
|---|-------------|----------------|
| 1 | `model_request`: inside the request-1 model step, right after its first text delta was NOTIFY'd | A model step that dies mid-stream is re-executed, not replayed half-done. The tool step that was already checkpointed is not called again (1 call). The test listens on `eap_run_deltas`: the crashed attempt sent 1 delta under attempt A, and the retried request streams the full final text under a different attempt B, both with `model_request` 1. That is what lets the UI drop the abandoned partial text. |
| 2 | `after_tool_call`: the ticket service returned, but `side_effects` is still `reserved` and the step is not checkpointed | The tool step is at-least-once: recovery calls the service again (2 calls) with the same idempotency key, and the service deduplicates it (1 ticket). The pre-crash `tool_call_id` is replayed, not regenerated. |
| 3a | `after_event_write` for `tool:*:result`: the event committed to `eap_app`, but `eap.append_event` has no checkpoint in `eap_dbos` | The re-executed append hits `ON CONFLICT (run_id, event_key)`. The stored row keeps its seq (3) and data, no seq is consumed, and nothing is duplicated. |
| 3b | `after_event_write` for `model:*:final` (first match: `model:0:final`, seq 1) | The same, for a model event written from the checkpointed response. The tool runs for the first time after recovery (1 call). |
| 4 | API: `runs` row committed, process dies before the enqueue | The orphan row stays `queued`, with no workflow and no events, while a live worker polls. Retrying the same POST returns 200 (the row already existed), enqueues `run_id` as the workflow id, and the run completes once. A further retry after completion is a no-op: one workflow, one runs row, the same events, 1 call. |
| 5 | `model_request` during turn 2 of a three-turn conversation (`test_conversation_crash.py`, `test:history` agent, no tools) | Turn 3 is refused with 409 while turn 2 recovers, and retrying turn 2's POST is idempotent (200). After recovery each turn appears once, in order. Turn 3's answer lists all three prompts, so it carries the fact from turn 1. Every turn's events are `start, model:0:final, end` and replay over SSE. Another tenant gets 404 on the conversation. Scenario 5 checks its own story, not the ticket invariants above. |

### Why scenario 4 kills a separate API process

The alternatives were a launcher that raises on its first call, or inserting the row with
`eap_run_store` and then POSTing. Raising runs the route's exception handler and returns a 503, so
the request ends normally instead of dying. Inserting directly skips the route's own insert. Killing
the process inside `launch` leaves exactly the state a real crash leaves: the route's own insert
committed and nothing else happened. The retry then goes through the real
`created=False → compare → launch` path.

# Milestone 3 (task 3.10): workflow crashes and MCP safety, whole-system

`test_workflow_crash.py` and `test_mcp_safety.py` start the **real API in its own process**
(`api_server.py`: uvicorn on a free 127.0.0.1 port, INFO logs to a file) and talk to it over real
HTTP (`Harness.start_api()` swaps `harness.client` for an HTTP client). Agents, workflows and MCP
servers are created and published over HTTP; runs are started, approved and streamed over HTTP.
The worker-level tests in `apps/worker/worker_tests/test_workflow_run_crash*.py` and
`test_mcp_*.py` enqueue directly or use an in-process app; these do not. Knobs:
`EAP_APPROVAL_POLL_SECONDS=1` (recorded once per run, so every process gets it; W4d and W4e record a
20 s hour and a 0.25 s poll instead, and W4e waits until that hour has passed). The suite takes
about 3.5 minutes; every API, worker, MCP
server and collector is stopped in teardown, and the
test's DBOS schema and tenant rows (including workflows and MCP servers) are deleted.

## Workflow invariants (`assert_exactly_once`)

1. The run ends once: the expected terminal status (`completed`, or `needs_attention` where a side
   effect's outcome is unknown), one `end` event, exactly one DBOS workflow with the run id, in
   SUCCESS, and no DBOS workflow prefixed by the run id other than its parallel branches
   (`{run_id}:{parallel}:{branch}`), each once, in SUCCESS.
2. Gap-free seq from 0 and no duplicate event_key across the parent and its branches; the
   expected story (exact order, or the exact key set plus ordering rules for parallel branches).
3. Side effects once: the expected number of `fake_external_tickets` rows and service calls, and
   exactly one child agent run per agent step that ran (the step_started event's child_run_id),
   each in the expected status (completed, or needs_attention when that child is the unknown side
   effect) and finished by one DBOS workflow with its own gap-free events; `GET /runs?parent_run_id=`
   lists exactly those. Nothing changes during a further 1 s settle.
4. SSE resume (`?after_seq=` and `Last-Event-ID`) from -1, 0, the last seq stored before the
   crash, the midpoint, last-1 and last returns exactly the database's events after that seq.

| # | Kill | Proves |
|---|------|--------|
| W1 | `workflow_mid_step` at `step:ask:started` (agent) / `step:t:started` (tool) | the step re-runs after recovery: completed once, 1 ticket, 2 child runs |
| W2 | `after_workflow_tool_call` at `step:t:call` | needs_attention once, the ticket service is not called again (1 call, 1 ticket) |
| W3 | inside branch a while its sibling ticket is held behind an approval; inside branch b before its tool; at the join; after branch b's tool call | branch a completes once after that approval (the ticket is not in flight at the kill); the others complete once, or needs_attention after the tool call; each branch workflow once, 1 ticket |
| W4 | `workflow_waiting`; approve over HTTP after the restart, or while no worker runs | the decision is never lost; completed once; a second approve or reject is 409 |
| W4c | SIGKILL of a healthy worker while a parallel branch's approval is in DBOS recv (newest operation is recv's `DBOS.sleep` timeout record; 600 s poll); approve while down | the branch workflow (the wake-up target) resumes; completed once |
| W4d | kill while waiting under a 20 s hour; restart; approve within a second of the recorded deadline | exactly one `step:hold:decision`; completed if that event is `step_approved`, failed `approval_timeout` if it is `step_timed_out`; the HTTP result matches; a second decide is 409. This is the race: the approve is sent while the run is still waiting |
| W4e | same 20 s hour and crash as W4d; the approve is sent only after the recorded deadline, once `step_timed_out` is stored | 409; exactly one `step:hold:decision`, type `step_timed_out`, timestamp inside the deadline window (deadline−0.1 s to deadline+1.0 s); failed `approval_timeout`; the later step never starts; a second decide is 409 |
| W7 | `after_tool_call` on an MCP tool reached through a workflow agent step (agent with `mcp_tools`) | the workflow and its child run each end needs_attention once; the server is called once and not contacted again |
| W5 | `after_child_run_insert`, then v2 of the agent is published over HTTP | the recovered child runs v1 (the version its row names) |
| W5b | `after_child_run_insert`, then the agent is archived (SQL) | recovery reloads v1 from the child row and completes; the archived check guards only the create path |
| W6a | the API process dies inside `launch_workflow` (`crashing_workflow_api.py run`) | the orphan stays queued; the same-run_id retry is 202 and completes once; a further retry is a no-op; other input is 409 |
| W6b | the API process dies inside `notify_approval` (`crashing_workflow_api.py approve`) | the committed decision is found by the poll; completed once |

## MCP safety

Names that should resolve to a private address are not left to real DNS or `/etc/hosts`.
`api_server.py` installs a test-only `socket.getaddrinfo` stub when `EAP_TEST_DNS_STUB` is set
(`name=addr[|addr],...`, several names comma-separated) and logs `EAP_TEST_DNS_STUB active`.
M7a uses it for an intranet name (10.0.0.5), a metadata name (169.254.169.254), a ULA name
(`fd00::5`), and a name with one public and one private record. Every other name still resolves
for real. The literal list also covers ULA (`fd00::`, `fc00::`) and 6to4 (`2002:`).

| # | What | Proves |
|---|------|--------|
| M7a | literal URLs (loopback, private, link-local/metadata, ULA `fd00`/`fc00`, 6to4 `2002:`, CGNAT, 0.0.0.0, IPv4-mapped and NAT64 IPv6, `127.1`, decimal and hex hosts, http, credentials) plus the stubbed DNS names above, without a dev allow host | 422 with the policy code (`address_blocked` for the private literals, ULA, 6to4, and stubbed names), the URL and the resolved address are not echoed, nothing is stored |
| M7b | API allows 127.0.0.1, the worker does not | the worker's own check refuses: 0 requests reach the server, run fails `mcp_tool_unavailable` |
| M8 | two header-secret sentinels, a server that echoes the headers back (as a result and as a tool error), OTLP exported to a local collector | the sentinels appear in no API response (create, get, list, test, a 422, PATCH-URL 422s, runs, events, SSE), neither process log, no OTLP trace or log export, no runs / execution_events / side_effects row and no row of the DBOS system schema, in plain, URL, hex or any-alignment base64 form |
| M9 | gzip result, 8 MB result, remote `$ref` in a listed schema | needs_attention after exactly one tools/call for the first two (identity encoding only); the `$ref` is never fetched |
| M10 | `after_tool_call` on a side-effecting MCP call | needs_attention once, the server is not contacted again, no secret in either worker log |
| M11 | an allowlisted server answers 302 and 307 to a counting listener on another 127.0.0.1 port | 0 connections reach that listener, through the API connection test (`redirect_refused`) and through a real agent run (`mcp_tool_unavailable`, worker inner code ends with `redirect_refused`, the same code as the API, also in the worker log); the location and port are not in the error |
