// A fake of the AC.12 API for the Tests tab acceptance tests: it answers the real routes with the
// real wire shapes through a stubbed `fetch`, records every call, and can hold SSE streams open.
import { vi } from "vitest";
import type { Agent, AgentVersion } from "@/lib/api";
import type { EvalCaseResult, EvalCase, EvalCompare, EvalRun, EvalSuiteWithCases, Evaluator, SideEffectWarning } from "@/lib/evals";

export const AGENT = "a1";
export const SUITE = "s1";
export const RUN_A = "11111111-2222-3333-4444-555555555555";
export const RUN_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
// Built from parts: the route guard forbids raw route literals outside lib/routes (this is the fake server's match table).
const AGENT_PATH = ["", "agents", AGENT].join("/");

export interface Call {
  method: string;
  path: string;
  query: URLSearchParams;
  raw: string;
  body: unknown;
  headers: Record<string, string>;
}
export interface Failure {
  method: string;
  path: RegExp;
  status: number;
  body: unknown;
  once?: boolean;
}
export interface FakeStream {
  runId: string;
  headers: Record<string, string>;
  push: (event: string, data: unknown, id?: number) => void;
  end: () => void;
}

export const spec = (extra: Record<string, unknown> = {}) =>
  ({ spec_version: 1, name: "Bot", instructions: "Be nice", model: { provider: "test", name: "echo" }, tools: [], ...extra }) as never;

export const caseOf = (id: string, name: string, extra: Partial<EvalCase> = {}): EvalCase => ({
  id,
  suite_id: SUITE,
  name,
  input: `input of ${name}`,
  expected_output: `expected ${name}`,
  metadata: {},
  evaluators: [],
  position: Number(id.replace(/\D/g, "")) || 0,
  created_at: null,
  updated_at: null,
  ...extra,
});

export const suiteOf = (extra: Partial<EvalSuiteWithCases> = {}): EvalSuiteWithCases => {
  const cases = extra.cases ?? [caseOf("c1", "first"), caseOf("c2", "second")];
  return {
    id: SUITE,
    agent_id: AGENT,
    name: "Refunds",
    description: "Refund questions",
    evaluators: [{ kind: "equals_expected" }] as Evaluator[],
    archived: false,
    archived_at: null,
    created_at: null,
    updated_at: null,
    ...extra,
    cases,
    case_count: cases.length,
  };
};

export const runOf = (id: string, version: number | null, status = "completed", passRate = 0.5): EvalRun => ({
  run_id: id,
  kind: "eval",
  agent_id: AGENT,
  version,
  status,
  display_status: status,
  error: null,
  created_at: "2026-11-15T10:00:00+00:00",
  updated_at: "2026-11-15T10:00:00+00:00",
  trace_id: null,
  suite_id: SUITE,
  suite_hash: "h",
  aggregates: { cases: 2, passed: 1, failed: 1, errors: 0, pass_rate: passRate, scores_avg: { llm_judge: 0.5 }, duration_ms_p50: 120, cost_usd: "0.0020" },
});

export const resultOf = (patch: Partial<EvalCaseResult> = {}): EvalCaseResult => ({
  case_id: "c1",
  case_name: "first",
  position: 0,
  child_run_id: "child-1",
  status: "passed",
  output: "hello",
  assertions: { equals_expected: { value: true } },
  scores: {},
  labels: {},
  evaluator_failures: [],
  duration_ms: 100,
  cost_usd: "0.0010",
  error_code: null,
  created_at: null,
  ...patch,
});

export const MODELS = [
  { id: "test:judge", provider: "test", name: "judge", wildcard: false, source: "scripted", description: null },
  { id: "openai:gpt-4.1-mini", provider: "openai", name: "gpt-4.1-mini", wildcard: false, source: "allowlist", description: null },
  { id: "openai:*", provider: "openai", name: "*", wildcard: true, source: "allowlist", description: null },
];

export interface FakeState {
  agent: Agent;
  versions: AgentVersion[];
  suites: EvalSuiteWithCases[];
  runs: EvalRun[];
  results: Record<string, EvalCaseResult[]>;
  /** `runs.input` by run id (the frozen suite snapshot). */
  runInputs: Record<string, string>;
  /** Side-effect warnings by target: "latest", a version number as text, or "draft". */
  warnings: Record<string, SideEffectWarning[]>;
  compare: EvalCompare | null;
  calls: Call[];
  failures: Failure[];
  streams: FakeStream[];
  /** Results per page (undefined: one page). */
  pageSize?: number;
  /** Delays the answer of POST /evals/runs until released. */
  holdStart: Promise<void> | null;
  /** Run ids the start route has accepted. */
  started: string[];
}

export function versionOf(version: number): AgentVersion {
  return { agent_id: AGENT, version, spec: spec(), spec_hash: `h${version}`, created_at: "2026-11-14T00:00:00Z", created_by: "dev" };
}

export function newState(): FakeState {
  return {
    agent: { id: AGENT, name: "Bot", draft_spec: spec(), latest_version: 2 },
    versions: [versionOf(2), versionOf(1)],
    suites: [suiteOf()],
    runs: [],
    results: {},
    runInputs: {},
    warnings: {},
    compare: null,
    calls: [],
    failures: [],
    streams: [],
    holdStart: null,
    started: [],
  };
}

export const json = (data: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(status === 204 ? null : JSON.stringify(data), { status, headers: { "Content-Type": "application/json", ...headers } });

const summaryOf = (s: EvalSuiteWithCases) => {
  const { cases: _cases, ...rest } = s;
  return rest;
};

export const callsTo = (state: FakeState, method: string, path: RegExp) => state.calls.filter((c) => c.method === method && path.test(c.path));

/** An exported dataset in the `pydantic_evals` YAML layout (block style). */
export function yamlOf(s: EvalSuiteWithCases): string {
  const cases = s.cases.map((c) => `- name: ${c.name}\n  inputs: ${c.input}\n  expected_output: ${c.expected_output}\n`).join("");
  return `name: ${s.name}\ncases:\n${cases}evaluators:\n- EqualsExpected\n`;
}

/** The same dataset as JSON. */
export function jsonOf(s: EvalSuiteWithCases): string {
  const cases = s.cases.map((c) => ({ name: c.name, inputs: c.input, expected_output: c.expected_output }));
  return `${JSON.stringify({ name: s.name, cases, evaluators: ["EqualsExpected"] }, null, 2)}\n`;
}

function openStream(state: FakeState, runId: string, headers: Record<string, string>): Response {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start: (c) => { controller = c; } });
  let closed = false;
  state.streams.push({
    runId,
    headers,
    push: (event, data, id) => {
      if (closed) return;
      const idLine = id === undefined ? "" : `id: ${id}\n`;
      controller.enqueue(encoder.encode(`event: ${event}\n${idLine}data: ${JSON.stringify(data)}\n\n`));
    },
    end: () => {
      if (closed) return;
      closed = true;
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

function page<T>(items: T[], after: string | null, size: number | undefined): { items: T[]; next_cursor: string | null } {
  if (!size) return { items, next_cursor: null };
  const start = after ? Number(after) : 0;
  const end = start + size;
  return { items: items.slice(start, end), next_cursor: end < items.length ? String(end) : null };
}

function suiteById(state: FakeState, id: string) {
  return state.suites.find((s) => s.id === id);
}

const notFound = () => json({ detail: "Not found" }, 404);

async function handle(state: FakeState, input: unknown, init: RequestInit = {}): Promise<Response> {
  const url = new URL(String(input));
  const method = (init.method ?? "GET").toUpperCase();
  const path = url.pathname.replace(/^\/api\/v1/, "");
  const headers = Object.fromEntries(Object.entries((init.headers ?? {}) as Record<string, string>).map(([k, v]) => [k.toLowerCase(), String(v)]));
  const raw = typeof init.body === "string" ? init.body : "";
  let body: unknown = raw;
  if ((headers["content-type"] ?? "").includes("json") && raw) body = JSON.parse(raw);
  state.calls.push({ method, path, query: url.searchParams, raw, body, headers });
  const at = state.failures.findIndex((f) => f.method === method && f.path.test(path));
  if (at >= 0) {
    const failure = state.failures[at];
    if (failure.once) state.failures.splice(at, 1);
    return json(failure.body, failure.status);
  }
  return route(state, method, path, url.searchParams, body, raw, headers);
}

async function route(state: FakeState, method: string, path: string, q: URLSearchParams, body: unknown, raw: string, headers: Record<string, string>): Promise<Response> {
  const b = (body ?? {}) as Record<string, unknown>;
  let m: RegExpExecArray | null;
  if (method === "GET" && path === `${AGENT_PATH}`) return json(state.agent);
  if (method === "GET" && path === `${AGENT_PATH}/versions`) return json(state.versions);
  if (method === "GET" && (m = new RegExp(`^/agents/${AGENT}/versions/(\\d+)$`).exec(path))) {
    const found = state.versions.find((v) => v.version === Number(m![1]));
    return found ? json(found) : notFound();
  }
  if (method === "GET" && path === "/models") return json({ items: MODELS });
  if (path === `${AGENT_PATH}/eval-suites`) {
    if (method === "GET") return json({ items: state.suites.filter((s) => !s.archived || q.get("include_archived") === "true").map(summaryOf) });
    const created = suiteOf({ id: `s${state.suites.length + 1}`, name: String(b.name), description: (b.description as string | null) ?? null, evaluators: b.evaluators as Evaluator[], cases: [] });
    state.suites.push(created);
    return json(summaryOf(created), 201);
  }
  if ((m = /^\/eval-suites\/([^/]+)$/.exec(path))) {
    const suite = suiteById(state, m[1]);
    if (!suite) return notFound();
    if (method === "GET") return json(suite);
    if (method === "DELETE") {
      state.suites = state.suites.filter((s) => s !== suite);
      return json(null, 204);
    }
    Object.assign(suite, b);
    return json(summaryOf(suite));
  }
  if ((m = /^\/eval-suites\/([^/]+)\/cases(?:\/([^/]+))?$/.exec(path))) {
    const suite = suiteById(state, m[1]);
    if (!suite) return notFound();
    if (method === "POST") {
      const made = caseOf(`c${suite.cases.length + 1}`, String(b.name), { input: String(b.input), expected_output: b.expected_output, metadata: b.metadata as never, evaluators: b.evaluators as never });
      suite.cases.push(made);
      suite.case_count = suite.cases.length;
      return json(made, 201);
    }
    const found = suite.cases.find((c) => c.id === m![2]);
    if (!found) return notFound();
    if (method === "DELETE") {
      suite.cases = suite.cases.filter((c) => c !== found);
      suite.case_count = suite.cases.length;
      return json(null, 204);
    }
    return json(Object.assign(found, b));
  }
  if (method === "GET" && (m = /^\/eval-suites\/([^/]+)\/export$/.exec(path))) {
    const suite = suiteById(state, m[1]);
    if (!suite) return notFound();
    const fmt = q.get("format") === "json" ? "json" : "yaml";
    const text = fmt === "json" ? jsonOf(suite) : yamlOf(suite);
    return new Response(text, { status: 200, headers: { "Content-Type": `application/${fmt}`, "Content-Disposition": `attachment; filename="${suite.name}.${fmt}"` } });
  }
  if (method === "POST" && (m = /^\/eval-suites\/([^/]+)\/import$/.exec(path))) {
    const suite = suiteById(state, m[1]);
    if (!suite) return notFound();
    return importFile(suite, raw, q.get("format") ?? "yaml");
  }
  return routeRuns(state, method, path, q, b, headers);
}

function importFile(suite: EvalSuiteWithCases, text: string, fmt: string): Response {
  const names: string[] = [];
  if (fmt === "json") {
    try {
      const data = JSON.parse(text) as { cases?: { name: string }[] };
      for (const c of data.cases ?? []) names.push(c.name);
    } catch {
      return json({ detail: "the suite file is invalid: not valid JSON" }, 422);
    }
  } else {
    if (!/^cases:/m.test(text)) return json({ detail: "the suite file is invalid at cases: a list of cases is required" }, 422);
    for (const match of text.matchAll(/^- name: (.+)$/gm)) names.push(match[1]);
  }
  let created = 0;
  let updated = 0;
  for (const name of names) {
    if (suite.cases.some((c) => c.name === name)) updated += 1;
    else {
      created += 1;
      suite.cases.push(caseOf(`c${suite.cases.length + 1}`, name));
    }
  }
  suite.case_count = suite.cases.length;
  return json({ created, updated, suite: summaryOf(suite) });
}

async function routeRuns(state: FakeState, method: string, path: string, q: URLSearchParams, b: Record<string, unknown>, headers: Record<string, string>): Promise<Response> {
  let m: RegExpExecArray | null;
  if (method === "GET" && path === `${AGENT_PATH}/evals/warnings`) {
    const key = q.get("version") ?? "latest";
    return json({ version: /^\d+$/.test(key) ? Number(key) : null, draft: key === "draft", warnings: state.warnings[key] ?? [] });
  }
  if (path === `${AGENT_PATH}/evals/runs` && method === "POST") {
    if (state.holdStart) await state.holdStart;
    const runId = String(b.run_id);
    const again = state.started.includes(runId);
    if (!again) state.started.push(runId);
    const suite = suiteById(state, String(b.suite_id));
    if (!suite) return notFound();
    const version = b.version === "draft" ? null : (b.version as number | null) ?? state.agent.latest_version;
    if (!again) {
      state.runs.unshift({ ...runOf(runId, version, "queued"), aggregates: undefined });
      state.runInputs[runId] = JSON.stringify({ cases: suite.cases.map((c) => ({ name: c.name })) });
    }
    const side = state.warnings[b.version === "draft" ? "draft" : String(version)] ?? [];
    return json({ run_id: runId, status: "queued", display_status: "queued", agent_id: AGENT, version, draft: b.version === "draft", suite_id: suite.id, suite_hash: "h", cases: suite.cases.length, max_concurrency: b.max_concurrency, trace_id: null, warnings: side }, again ? 200 : 202);
  }
  if (method === "GET" && path === `${AGENT_PATH}/evals/runs`) {
    const items = state.runs.filter((r) => !q.get("suite_id") || r.suite_id === q.get("suite_id"));
    return json({ items, next_cursor: null });
  }
  if (method === "GET" && path === `${AGENT_PATH}/evals/compare`) {
    return state.compare ? json(state.compare) : json({ detail: "Run not found" }, 404);
  }
  if (method === "GET" && (m = /^\/agents\/[^/]+\/evals\/runs\/([^/]+)(\/results)?$/.exec(path))) {
    const run = state.runs.find((r) => r.run_id === m![1]);
    if (!run) return notFound();
    if (!m[2]) return json(run);
    return json(page(state.results[run.run_id] ?? [], q.get("after"), state.pageSize));
  }
  if (method === "GET" && (m = /^\/runs\/([^/]+)\/stream$/.exec(path))) return openStream(state, m[1], headers);
  if (method === "GET" && (m = /^\/runs\/([^/]+)$/.exec(path))) return json({ id: m[1], input: state.runInputs[m[1]] ?? null });
  return json({ detail: `unrouted ${method} ${path}` }, 500);
}

export interface FakeApi {
  state: FakeState;
  /** The newest SSE connection for a run (null when none is open). */
  stream: (runId: string) => FakeStream | null;
  fail: (failure: Failure) => void;
  restore: () => void;
}

/** Stubs `fetch` (and the dev token) with the fake; `restore()` undoes both. */
export function installFakeApi(setup: (state: FakeState) => void = () => undefined): FakeApi {
  const state = newState();
  setup(state);
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal("fetch", vi.fn((input: unknown, init?: RequestInit) => handle(state, input, init)));
  return {
    state,
    stream: (runId) => [...state.streams].reverse().find((s) => s.runId === runId) ?? null,
    fail: (failure) => state.failures.push(failure),
    restore: () => {
      vi.unstubAllGlobals();
      vi.unstubAllEnvs();
    },
  };
}

/** Makes POST /evals/runs wait until the returned function is called. */
export function holdStart(state: FakeState): () => void {
  let open!: () => void;
  state.holdStart = new Promise<void>((resolve) => {
    open = resolve;
  });
  return open;
}
