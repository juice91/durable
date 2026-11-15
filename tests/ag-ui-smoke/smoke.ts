/**
 * AG-UI smoke test (AGUI.14): the official TypeScript client, `@ag-ui/client@1.0.3`, against a
 * running PydanticOS API and worker with the scripted `test:*` models.
 *
 * Scenarios (EAP_SMOKE_SCENARIOS=a,b runs a subset):
 *   chat                 text streaming through `ResumableHttpAgent` (Studio's client, D7)
 *   attach               GET attach route: a full replay via `connectAgent()`, and a continuation (`?after=`)
 *   resume               a network cut mid-run, then re-POST with `Last-Event-ID` and splicing
 *   mcp_tool             an MCP tool call and result, executed once on the MCP server
 *   frontend_tool        a client tool: the turn ends pending, the client answers, the next run continues
 *   previous_turn        a plain `HttpAgent` drops its stream; the next message waits, heals, then runs
 *   structured           a structured-output agent: `RUN_FINISHED.result` plus one JSON text message
 *   tool_approval        a gated agent tool: interrupt, resume (approve), the tool runs once on the server
 *   tool_approval_copilotkit  the same, with the tool message CopilotKit 1.83 adds to its resume body
 *   workflow_approval    workflow steps and state, an approval step as an interrupt, resume
 *
 * Every request body and every received event is validated with the `@ag-ui/core` schemas, and
 * every stream with the lifecycle rules in wire.ts. Env: EAP_SMOKE_API_URL (the `/api/v1` base),
 * EAP_SMOKE_MCP_URL (mcp_server.py's `/mcp`), EAP_SMOKE_TENANT (default: a fresh `smoke<hex>`).
 * Start a stack with run.sh, which sets these.
 */
import { randomUUID } from "node:crypto";
import assert from "node:assert/strict";

import { HttpAgent, type Message } from "@ag-ui/client";

import { ResumableHttpAgent } from "../../apps/studio/src/lib/agui/resumableHttpAgent";
import { Api, mcpCalls, type ThreadView } from "./api";
import { Wire, conformanceErrors, eventsOf, typesOf, type Exchange } from "./wire";

const API_URL = (process.env.EAP_SMOKE_API_URL ?? "http://127.0.0.1:8200/api/v1").replace(/\/+$/, "");
const MCP_URL = process.env.EAP_SMOKE_MCP_URL ?? "http://127.0.0.1:8209/mcp";
const TENANT = process.env.EAP_SMOKE_TENANT ?? `smoke${randomUUID().replace(/-/g, "").slice(0, 10)}`;
const SCENARIO_TIMEOUT_MS = 90_000;
const MCP_SERVER = "smoke";

const api = new Api(API_URL, TENANT);

interface Setup {
  key: string;
  echo: string;
  mcp: string;
  frontend: string;
  structured: string;
  gated: string;
  workflow: string;
}

let setup: Setup;

// --------------------------------------------------------------------------- helpers

function resumable(url: string, wire: Wire, threadId: string): ResumableHttpAgent {
  return new ResumableHttpAgent({
    url,
    threadId,
    headers: { Authorization: `Bearer ${setup.key}` },
    fetch: wire.fetch,
    storage: null,
    // Short retries: the smoke cuts the connection on purpose.
    sleep: (ms, signal) => new Promise((resolve) => {
      const timer = setTimeout(resolve, Math.min(ms, 200));
      signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
    }),
  });
}

function plain(url: string, wire: Wire, threadId: string): HttpAgent {
  return new HttpAgent({ url, threadId, headers: { Authorization: `Bearer ${setup.key}` }, fetch: wire.fetch });
}

function user(content: string, id = `u-${randomUUID()}`): Message {
  return { id, role: "user", content };
}

async function conformant(wire: Wire): Promise<void> {
  await wire.settled();
  const errors = conformanceErrors(wire);
  assert.deepEqual(errors, [], `AG-UI conformance:\n${errors.join("\n")}`);
}

function only<T>(items: T[], what: string): T {
  assert.equal(items.length, 1, `expected exactly one ${what}, got ${items.length}`);
  return items[0];
}

function events(exchange: Exchange, type: string): Record<string, unknown>[] {
  return eventsOf(exchange).filter((e) => e.type === type);
}

function custom(exchange: Exchange, name: string): Record<string, unknown>[] {
  return eventsOf(exchange).filter((e) => e.type === "CUSTOM" && e.name === name);
}

function finished(exchange: Exchange): Record<string, unknown> {
  const last = eventsOf(exchange).at(-1);
  assert.equal(last?.type, "RUN_FINISHED", `the stream must end with RUN_FINISHED, got ${JSON.stringify(last)}`);
  return last!;
}

function outcome(exchange: Exchange): Record<string, unknown> {
  return (finished(exchange).outcome as Record<string, unknown> | undefined) ?? {};
}

function textOf(messages: Message[], role = "assistant"): string[] {
  return messages.filter((m) => m.role === role).map((m) => String((m as { content?: unknown }).content ?? ""));
}

/** The client's messages as the server's thread view shows them: ids, roles and text. */
function comparable(messages: Array<Record<string, unknown>>): Array<[string, string, string]> {
  return messages
    .filter((m) => m.role === "user" || m.role === "assistant")
    .map((m) => [String(m.id), String(m.role), typeof m.content === "string" ? m.content : ""]);
}

/**
 * The client's messages equal the server's thread view. `ordered: false` compares them as a set:
 * `@ag-ui/client` merges a MESSAGES_SNAPSHOT by id (messages it holds keep their order, new ones are
 * appended), so after previous-turn healing a plain client holds the missed answer after the new
 * user message, while the server's (and the snapshot's) order is the thread's.
 */
async function threadMatches(agentId: string, threadId: string, messages: Message[], ordered = true): Promise<ThreadView> {
  const view = await api.thread(agentId, threadId, setup.key);
  const mine = comparable(messages as unknown as Array<Record<string, unknown>>);
  const theirs = comparable(view.messages);
  if (ordered) assert.deepEqual(mine, theirs);
  else assert.deepEqual([...mine].sort(), [...theirs].sort());
  const ids = messages.map((m) => m.id);
  assert.equal(new Set(ids).size, ids.length, `duplicate message ids: ${ids.join(",")}`);
  return view;
}

function runIdOf(exchange: Exchange): string {
  const info = only(custom(exchange, "pydanticos.run"), "CUSTOM pydanticos.run");
  return String((info.value as Record<string, unknown>).run_id);
}

// --------------------------------------------------------------------------- scenarios

let chatRun: { threadId: string; runId: string; lastId: string } | undefined;

async function chat(): Promise<void> {
  const wire = new Wire();
  const threadId = `chat-${randomUUID()}`;
  const runId = randomUUID();
  const agent = resumable(api.agentUrl(setup.echo), wire, threadId);
  agent.addMessage(user("hello from the smoke test"));
  await agent.runAgent({ runId });
  await conformant(wire);
  const stream = only(wire.streams(), "stream");
  const types = typesOf(stream);
  assert.equal(types[0], "RUN_STARTED");
  assert.equal(types[1], "CUSTOM:pydanticos.run");
  for (const t of ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"]) assert.ok(types.includes(t), `${t} missing: ${types}`);
  assert.equal((outcome(stream).type), "success");
  assert.match(textOf(agent.messages).at(-1) ?? "", /hello from the smoke test/);
  await threadMatches(setup.echo, threadId, agent.messages);
  const lastId = stream.frames.filter((f) => f.id).at(-1)?.id;
  assert.ok(lastId?.startsWith(`${runId}:`), `frame ids are {runId}:{seq}, got ${lastId}`);
  if (!lastId) return;
  chatRun = { threadId, runId, lastId };
}

async function attach(): Promise<void> {
  if (!chatRun) await chat();
  const { threadId, runId, lastId } = chatRun!;
  // A full replay through the client's connect() (the GET attach route), as after a reload.
  const wire = new Wire();
  const agent = resumable(api.agentUrl(setup.echo), wire, threadId);
  agent.attachRunId = runId;
  await agent.connectAgent();
  await conformant(wire);
  const stream = only(wire.streams(), "stream");
  assert.equal(stream.method, "GET");
  assert.ok(stream.url.endsWith(`/threads/${threadId}/runs/${runId}/stream`), stream.url);
  const types = typesOf(stream);
  assert.deepEqual(types.slice(0, 3), ["RUN_STARTED", "CUSTOM:pydanticos.run", "MESSAGES_SNAPSHOT"]);
  assert.equal(outcome(stream).type, "success");
  await threadMatches(setup.echo, threadId, agent.messages);
  // A continuation after the last frame the client holds: no RUN_STARTED, snapshot first, no frames
  // before the cursor, then the terminal event.
  const seq = Number(lastId.slice(lastId.lastIndexOf(":") + 1));
  const before = seq - 1;
  const cont = new Wire();
  const response = await cont.fetch(`${api.agentUrl(setup.echo)}/threads/${threadId}/runs/${runId}/stream?after=${before}`, {
    headers: { Authorization: `Bearer ${setup.key}`, Accept: "text/event-stream" },
  });
  await response.text();
  await conformant(cont);
  const c = only(cont.streams(), "stream");
  const ctypes = typesOf(c);
  assert.notEqual(ctypes[0], "RUN_STARTED", `a continuation has no RUN_STARTED: ${ctypes}`);
  assert.deepEqual(ctypes.slice(0, 2), ["CUSTOM:pydanticos.run", "MESSAGES_SNAPSHOT"]);
  assert.equal(ctypes.at(-1), "RUN_FINISHED");
  for (const f of c.frames.filter((x) => x.id)) {
    assert.ok(Number(f.id!.slice(f.id!.lastIndexOf(":") + 1)) > before, `frame ${f.id} is not after ${before}`);
  }
  // A cursor beyond the run is refused before any stream (D7).
  const bad = await fetch(`${api.agentUrl(setup.echo)}/threads/${threadId}/runs/${runId}/stream`, {
    headers: { Authorization: `Bearer ${setup.key}`, "Last-Event-ID": `${runId}:999999` },
  });
  assert.equal(bad.status, 400);
  await bad.body?.cancel();
}

async function resumeOnce(cutAfter: "content" | "id"): Promise<void> {
  const wire = new Wire();
  const threadId = `resume-${cutAfter}-${randomUUID()}`;
  const runId = randomUUID();
  let cutId: string | undefined;
  wire.cut = (frame) => {
    if (frame.event?.type === "RUN_FINISHED") return false;
    if (cutAfter === "content" ? frame.event?.type === "TEXT_MESSAGE_CONTENT" : frame.id !== undefined) {
      cutId = frame.id;
      return true;
    }
    return false;
  };
  const agent = resumable(api.agentUrl(setup.echo), wire, threadId);
  agent.addMessage(user(`please resume this long enough answer after a cut (${cutAfter})`));
  await agent.runAgent({ runId });
  await conformant(wire);
  const streams = wire.streams();
  assert.ok(streams.length >= 2, `expected a re-POST after the cut, got ${streams.length} stream(s)`);
  assert.ok(streams[0].cut, "the first stream was cut");
  const second = streams[1];
  assert.equal(second.method, "POST");
  assert.deepEqual(second.body, streams[0].body, "the re-POST sends the identical RunAgentInput");
  const header = second.headers["last-event-id"];
  assert.match(header ?? "", new RegExp(`^${runId}:-?\\d+$`), `Last-Event-ID: ${header}`);
  if (cutAfter === "id") assert.equal(header, cutId);
  const types = typesOf(second);
  assert.notEqual(types[0], "RUN_STARTED", `a continuation has no RUN_STARTED: ${types}`);
  assert.ok(types.includes("MESSAGES_SNAPSHOT"), `the continuation resyncs with a snapshot: ${types}`);
  assert.equal(outcome(second).type, "success");
  // Frames after the cursor only: no frame id of the first stream repeats.
  const firstIds = new Set(streams[0].frames.flatMap((f) => (f.id ? [f.id] : [])));
  for (const f of second.frames) if (f.id) assert.ok(!firstIds.has(f.id), `frame ${f.id} sent twice`);
  assert.match(textOf(agent.messages).at(-1) ?? "", /resume this long enough answer/);
  await threadMatches(setup.echo, threadId, agent.messages);
}

async function resume(): Promise<void> {
  await resumeOnce("content");
  await resumeOnce("id");
}

async function mcpTool(): Promise<void> {
  const wire = new Wire();
  const threadId = `mcp-${randomUUID()}`;
  const orderId = `A-${randomUUID().slice(0, 8)}`;
  const agent = resumable(api.agentUrl(setup.mcp), wire, threadId);
  agent.addMessage(user(JSON.stringify({ tool: "lookup_order", args: { order_id: orderId } })));
  await agent.runAgent({ runId: randomUUID() });
  await conformant(wire);
  const stream = only(wire.streams(), "stream");
  const start = only(events(stream, "TOOL_CALL_START"), "TOOL_CALL_START");
  assert.match(String(start.toolCallName), /^mcp__.*lookup_order$/);
  const args = events(stream, "TOOL_CALL_ARGS").filter((e) => e.toolCallId === start.toolCallId).map((e) => e.delta).join("");
  assert.deepEqual(JSON.parse(args), { order_id: orderId });
  const result = only(events(stream, "TOOL_CALL_RESULT"), "TOOL_CALL_RESULT");
  assert.equal(result.toolCallId, start.toolCallId);
  assert.match(String(result.content), /shipped/);
  assert.equal(outcome(stream).type, "success");
  assert.match(textOf(agent.messages).at(-1) ?? "", /^MCP result: /);
  const calls = (await mcpCalls(MCP_URL)).filter((c) => c.args.order_id === orderId);
  assert.equal(calls.length, 1, "the MCP tool ran exactly once");
  // The client holds the call and its result as the server's thread does.
  const toolMessage = agent.messages.find((m) => m.role === "tool");
  assert.equal((toolMessage as { toolCallId?: string } | undefined)?.toolCallId, start.toolCallId);
  await threadMatches(setup.mcp, threadId, agent.messages);
}

const WEATHER_TOOL = {
  name: "get_weather",
  description: "The weather in a city (runs in the browser).",
  parameters: { type: "object", properties: { city: { type: "string" } }, required: ["city"], additionalProperties: false },
};

async function frontendTool(): Promise<void> {
  const wire = new Wire();
  const threadId = `frontend-${randomUUID()}`;
  const agent = plain(api.agentUrl(setup.frontend), wire, threadId);
  agent.addMessage(user("What is the weather?"));
  let pending: string[] = [];
  await agent.runAgent(
    { runId: randomUUID(), tools: [WEATHER_TOOL] },
    { onRunFinishedEvent: (p) => { if (p.outcome === "success") pending = p.pendingToolCallIds; } },
  );
  const first = only(wire.streams(), "stream");
  const start = only(events(first, "TOOL_CALL_START"), "TOOL_CALL_START");
  assert.equal(start.toolCallName, "get_weather");
  assert.deepEqual(events(first, "TOOL_CALL_RESULT"), [], "a client tool never runs on the server");
  assert.equal(outcome(first).type, "success");
  assert.deepEqual(outcome(first).pendingToolCallIds, [start.toolCallId]);
  assert.deepEqual(pending, [start.toolCallId]);
  const args = JSON.parse(events(first, "TOOL_CALL_ARGS").map((e) => e.delta).join("")) as { city: string };
  assert.equal(typeof args.city, "string");
  // The browser runs the tool and answers in the next run.
  agent.addMessage({ id: `tool-${randomUUID()}`, role: "tool", toolCallId: String(start.toolCallId), content: `sunny in ${args.city}` });
  await agent.runAgent({ runId: randomUUID(), tools: [WEATHER_TOOL] });
  await conformant(wire);
  const second = wire.streams()[1];
  assert.ok(second, "the second run streamed");
  assert.deepEqual(events(second, "TOOL_CALL_RESULT"), [], "the client's own result is not re-emitted live");
  assert.equal(outcome(second).type, "success");
  assert.equal(textOf(agent.messages).at(-1), `tool said: sunny in ${args.city}`);
}

async function previousTurn(): Promise<void> {
  const wire = new Wire();
  const threadId = `heal-${randomUUID()}`;
  const slowOrder = `P-${randomUUID().slice(0, 8)}`;
  const agent = plain(api.agentUrl(setup.mcp), wire, threadId);
  agent.addMessage(user(JSON.stringify({ tool: "slow_lookup", args: { order_id: slowOrder, seconds: 5 } })));
  // A plain HttpAgent (e.g. CopilotKit) that loses its stream right after RUN_STARTED.
  await agent
    .runAgent({ runId: randomUUID() }, { onRunStartedEvent: () => { setTimeout(() => agent.abortRun(), 100); } })
    .catch(() => undefined);
  await wire.settled();
  assert.ok(!eventsOf(wire.exchanges[0]).some((e) => e.type === "RUN_FINISHED"), "the first stream was dropped before the end");
  // The next message while the first turn still runs: wait, heal with the missed answer, then run.
  // (test:mcp answers a later turn from the history's tool return, so the second turn is text only.)
  const healAgent = plain(api.agentUrl(setup.mcp), wire, threadId);
  const secondRun = randomUUID();
  healAgent.setMessages([...agent.messages, user("and the second message?")]);
  await healAgent.runAgent({ runId: secondRun });
  await wire.settled();
  const second = wire.exchanges[1];
  const types = typesOf(second);
  assert.deepEqual(types.slice(0, 2), ["RUN_STARTED", "CUSTOM:pydanticos.waiting"], `${types}`);
  const snapshot = events(second, "MESSAGES_SNAPSHOT")[0] as { messages: Array<Record<string, unknown>> } | undefined;
  assert.ok(snapshot, `the healed stream carries a snapshot: ${types}`);
  assert.ok(
    snapshot.messages.some((m) => m.role === "assistant" && String(m.content ?? "").includes(slowOrder)),
    "the snapshot holds the missed answer",
  );
  const own = events(second, "TEXT_MESSAGE_START").map((e) => String(e.messageId));
  assert.deepEqual(own, [`${secondRun}-m0`], `the new turn streams its own answer: ${types}`);
  assert.ok(types.indexOf("MESSAGES_SNAPSHOT") < types.indexOf("TEXT_MESSAGE_START"), `the snapshot comes first: ${types}`);
  assert.equal(outcome(second).type, "success");
  const answers = healAgent.messages.filter((m) => m.role === "assistant");
  assert.ok(answers.some((m) => String(m.content ?? "").includes(slowOrder)), "the client now holds the missed answer");
  assert.equal(answers.at(-1)?.id, `${secondRun}-m0`, "then the new turn's answer");
  const calls = await mcpCalls(MCP_URL);
  assert.equal(calls.filter((c) => c.args.order_id === slowOrder).length, 1, "the first turn ran once");
  // The dropped first stream is checked only up to the drop.
  wire.exchanges[0].cut = true;
  await conformant(wire);
  const view = await threadMatches(setup.mcp, threadId, healAgent.messages, false);
  // The snapshot is the thread in the server's order: history, the missed answer, the new input.
  const snapshotIds = snapshot.messages.map((m) => String(m.id));
  assert.deepEqual(snapshotIds, view.messages.map((m) => m.id).slice(0, snapshotIds.length));
  assert.equal(snapshot.messages.at(-1)?.content, "and the second message?");
}

const TRIAGE_SCHEMA = {
  type: "object",
  properties: {
    priority: { type: "integer", minimum: 1, maximum: 3 },
    category: { type: "string", enum: ["bug", "feature", "question"] },
  },
  required: ["priority", "category"],
  additionalProperties: false,
};

async function structured(): Promise<void> {
  const wire = new Wire();
  const agent = plain(api.agentUrl(setup.structured), wire, `structured-${randomUUID()}`);
  agent.addMessage(user("The login page crashes on submit."));
  const result = await agent.runAgent({ runId: randomUUID() });
  await conformant(wire);
  const stream = only(wire.streams(), "stream");
  const done = finished(stream);
  assert.equal(outcome(stream).type, "success");
  assert.deepEqual(done.result, { priority: 1, category: "bug" });
  assert.deepEqual(result.result, done.result, "the client returns RUN_FINISHED.result");
  only(events(stream, "TEXT_MESSAGE_START"), "TEXT_MESSAGE_START");
  assert.ok(!events(stream, "TOOL_CALL_START").some((e) => e.toolCallName === "final_result"), "no final_result tool call");
  assert.deepEqual(JSON.parse(textOf(agent.messages).at(-1) ?? "null"), done.result, "the one text message is the JSON");
}

async function toolApproval(): Promise<void> {
  const wire = new Wire();
  const threadId = `approval-${randomUUID()}`;
  const agent = plain(api.agentUrl(setup.gated), wire, threadId);
  agent.addMessage(user(JSON.stringify({ tool: "tickets.create", args: { title: "Smoke ticket", body: "From the AG-UI smoke." } })));
  await agent.runAgent({ runId: randomUUID() });
  const first = only(wire.streams(), "stream");
  const call = only(events(first, "TOOL_CALL_START"), "TOOL_CALL_START");
  const o = outcome(first);
  assert.equal(o.type, "interrupt");
  const interrupt = only(o.interrupts as Array<Record<string, unknown>>, "interrupt");
  assert.equal(interrupt.reason, "tool_call");
  assert.equal(interrupt.toolCallId, call.toolCallId);
  const nativeRun = runIdOf(first);
  assert.equal(interrupt.id, `approval:${nativeRun}:tool:${String(call.toolCallId)}`);
  assert.ok(interrupt.responseSchema, "the interrupt carries Pydantic AI's response schema");
  assert.equal(custom(first, "pydanticos.approval_required").length, 1, "the CUSTOM fallback announces it");
  assert.deepEqual(events(first, "TOOL_CALL_RESULT"), [], "nothing ran before the decision");
  assert.equal(agent.pendingInterrupts.length, 1);
  // Resume: approve. The same durable run continues and the tool runs once, on the server.
  await agent.runAgent({
    runId: randomUUID(),
    resume: [{ interruptId: String(interrupt.id), status: "resolved", payload: { approved: true, reason: "smoke approves" } }],
  });
  await conformant(wire);
  const second = wire.streams()[1];
  assert.ok(second, "the resume run streamed");
  const decided = only(custom(second, "pydanticos.approval_decided"), "approval_decided");
  assert.equal((decided.value as Record<string, unknown>).decision, "approved");
  const result = only(events(second, "TOOL_CALL_RESULT"), "TOOL_CALL_RESULT");
  assert.equal(result.toolCallId, call.toolCallId);
  assert.equal(outcome(second).type, "success");
  assert.match(textOf(agent.messages).at(-1) ?? "", /outcome=success/);
  assert.equal(runIdOf(second), nativeRun, "the resume continues the same durable run");
  const native = await api.runEvents(nativeRun);
  const results = native.filter((e) => e.type === "tool_result" && e.data?.tool_call_id === call.toolCallId);
  assert.equal(results.length, 1, "the gated tool ran exactly once");
  assert.equal(agent.pendingInterrupts.length, 0);
}

/**
 * The resume body CopilotKit 1.83 sends (`useInterrupt` resolving a `tool_call` interrupt, found by
 * AGUI.16): the resume entry AND a `tool` message answering the gated server call with the
 * payload. The server drops that echo (the resume entry alone decides) instead of a 422.
 */
async function toolApprovalCopilotKit(): Promise<void> {
  const wire = new Wire();
  const threadId = `approval-ck-${randomUUID()}`;
  const agent = plain(api.agentUrl(setup.gated), wire, threadId);
  agent.addMessage(user(JSON.stringify({ tool: "tickets.create", args: { title: "CopilotKit ticket", body: "Echoed resume." } })));
  await agent.runAgent({ runId: randomUUID() });
  const first = only(wire.streams(), "stream");
  const call = only(events(first, "TOOL_CALL_START"), "TOOL_CALL_START");
  const interrupt = only(outcome(first).interrupts as Array<Record<string, unknown>>, "interrupt");
  const nativeRun = runIdOf(first);
  const payload = { approved: true };
  agent.addMessage({ id: `t-${randomUUID()}`, role: "tool", toolCallId: String(call.toolCallId), content: JSON.stringify(payload) });
  await agent.runAgent({ runId: randomUUID(), resume: [{ interruptId: String(interrupt.id), status: "resolved", payload }] });
  await conformant(wire);
  const second = wire.streams()[1];
  assert.ok(second, "the resume run streamed (no 422 for the echoed tool message)");
  const decided = only(custom(second, "pydanticos.approval_decided"), "approval_decided");
  assert.equal((decided.value as Record<string, unknown>).decision, "approved");
  assert.equal(only(events(second, "TOOL_CALL_RESULT"), "TOOL_CALL_RESULT").toolCallId, call.toolCallId);
  assert.equal(outcome(second).type, "success");
  assert.equal(runIdOf(second), nativeRun, "the resume continues the same durable run");
  const native = await api.runEvents(nativeRun);
  assert.equal(native.filter((e) => e.type === "tool_result" && e.data?.tool_call_id === call.toolCallId).length, 1, "the gated tool ran exactly once");
  assert.equal(native.filter((e) => e.type === "tool_approved").length, 1, "one decision");
}

async function workflowApproval(): Promise<void> {
  const wire = new Wire();
  const threadId = `wf-${randomUUID()}`;
  const agent = plain(api.workflowUrl(setup.workflow), wire, threadId);
  await agent.runAgent({ runId: randomUUID(), forwardedProps: { pydanticos: { input: { q: "smoke workflow" } } } });
  const first = only(wire.streams(), "stream");
  const types = typesOf(first);
  for (const t of ["STATE_SNAPSHOT", "STEP_STARTED", "STEP_FINISHED", "STATE_DELTA"]) assert.ok(types.includes(t), `${t} missing: ${types}`);
  assert.ok(events(first, "STEP_STARTED").some((e) => e.stepName === "ask"));
  const o = outcome(first);
  assert.equal(o.type, "interrupt");
  const interrupt = only(o.interrupts as Array<Record<string, unknown>>, "interrupt");
  assert.equal(interrupt.reason, "confirmation");
  assert.match(String(interrupt.id), /^approval:[0-9a-f-]+:appr$/);
  assert.ok(interrupt.expiresAt, "the interrupt names its deadline");
  assert.equal(custom(first, "pydanticos.approval_required").length, 1);
  assert.equal(agent.pendingInterrupts.length, 1);
  await agent.runAgent({
    runId: randomUUID(),
    resume: [{ interruptId: String(interrupt.id), status: "resolved", payload: { approved: true } }],
  });
  await conformant(wire);
  const second = wire.streams()[1];
  assert.ok(second, "the resume run streamed");
  assert.equal(runIdOf(second), runIdOf(first), "the resume continues the same workflow run");
  assert.equal(outcome(second).type, "success", `${typesOf(second)}`);
  assert.ok(events(second, "STATE_DELTA").length > 0, "the resume shows the step decision as state");
  assert.equal(agent.pendingInterrupts.length, 0);
}

// --------------------------------------------------------------------------- setup and runner

async function prepare(): Promise<void> {
  await api.registerMcpServer(MCP_SERVER, MCP_URL);
  const echo = await api.publishAgent({ name: "Smoke echo", instructions: "Echo.", model: { provider: "test", name: "echo" } });
  const mcp = await api.publishAgent({
    name: "Smoke MCP",
    instructions: "Use the MCP tools.",
    model: { provider: "test", name: "mcp" },
    mcp_tools: [
      { server: MCP_SERVER, tool: "lookup_order" },
      { server: MCP_SERVER, tool: "slow_lookup" },
    ],
  });
  const frontend = await api.publishAgent({
    name: "Smoke frontend tool",
    instructions: "Use the browser's tools.",
    model: { provider: "test", name: "frontend_tool" },
  });
  const structuredAgent = await api.publishAgent({
    name: "Smoke structured",
    instructions: "Triage the ticket.",
    model: { provider: "test", name: "structured" },
    output: { json_schema: TRIAGE_SCHEMA, name: "Triage" },
  });
  const gated = await api.publishAgent({
    name: "Smoke gated",
    instructions: "Create tickets when asked.",
    model: { provider: "test", name: "approval" },
    tools: ["tickets.create"],
    approvals: { tools: ["tickets.create"] },
  });
  const workflow = await api.publishWorkflow({
    name: "Smoke approval workflow",
    input_schema: { type: "object", properties: { q: { type: "string" } }, required: ["q"], additionalProperties: false },
    start: "ask",
    steps: [
      { id: "ask", type: "agent", agent_id: echo, input: { prompt: { $ref: "input.q" } }, next: "appr" },
      { id: "appr", type: "approval", prompt: "Escalate?", timeout_hours: 1 },
    ],
  });
  const key = await api.createApiKey("AG-UI smoke");
  setup = { key, echo, mcp, frontend, structured: structuredAgent, gated, workflow };
}

const SCENARIOS: Record<string, () => Promise<void>> = {
  chat,
  attach,
  resume,
  mcp_tool: mcpTool,
  frontend_tool: frontendTool,
  previous_turn: previousTurn,
  structured,
  tool_approval: toolApproval,
  tool_approval_copilotkit: toolApprovalCopilotKit,
  workflow_approval: workflowApproval,
};

function withTimeout<T>(promise: Promise<T>, ms: number, what: string): Promise<T> {
  let timer: NodeJS.Timeout;
  return Promise.race([
    promise.finally(() => clearTimeout(timer)),
    new Promise<T>((_, reject) => { timer = setTimeout(() => reject(new Error(`${what} timed out after ${ms} ms`)), ms); }),
  ]);
}

async function main(): Promise<void> {
  const wanted = (process.env.EAP_SMOKE_SCENARIOS ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  const unknown = wanted.filter((w) => !(w in SCENARIOS));
  if (unknown.length) throw new Error(`unknown scenarios: ${unknown.join(", ")}`);
  const names = wanted.length ? wanted : Object.keys(SCENARIOS);
  console.log(`AG-UI smoke: ${API_URL}, tenant ${TENANT}`);
  await prepare();
  let failed = 0;
  for (const name of names) {
    const started = Date.now();
    try {
      await withTimeout(SCENARIOS[name](), SCENARIO_TIMEOUT_MS, name);
      console.log(`PASS ${name} (${Date.now() - started} ms)`);
    } catch (error) {
      failed += 1;
      console.log(`FAIL ${name} (${Date.now() - started} ms)\n  ${String((error as Error)?.stack ?? error).replace(/\n/g, "\n  ")}`);
    }
  }
  console.log(failed ? `${failed} of ${names.length} scenarios FAILED` : `all ${names.length} scenarios passed`);
  process.exitCode = failed ? 1 : 0;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
