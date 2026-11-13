import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProcessorsPage from "./page";
import { ProcessorDetail } from "@/components/processors/ProcessorDetail";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations } from "@/test/a11y";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a>,
}));

const schema = (properties: Record<string, unknown>, required?: string[]) => ({ type: "object", properties, ...(required ? { required } : {}) });
const CATALOG = {
  items: [
    { id: "pii_redact", title: "PII redaction", description: "Detects personal data.", phases: ["input", "output"], actions: ["redact", "block"], deterministic: true,
      config_schema: schema({ kinds: { type: "array", default: ["email"], items: { type: "string", enum: ["email", "phone"] } }, action: { type: "string", default: "redact", enum: ["redact", "block"] } }) },
    { id: "regex_blocklist", title: "Regex blocklist", description: "Blocks text matching patterns.", phases: ["input", "output"], actions: ["block"], deterministic: true,
      config_schema: schema({ patterns: { type: "array", items: { type: "string" } }, action: { type: "string", default: "block", enum: ["block"] } }) },
    { id: "max_length", title: "Maximum length", description: "Blocks long text.", phases: ["input"], actions: ["block", "truncate"], deterministic: true,
      config_schema: schema({ max_chars: { type: "integer", minimum: 1, default: 5 }, action: { type: "string", default: "block", enum: ["block", "truncate"] } }, ["max_chars"]) },
    { id: "moderation", title: "Moderation", description: "Asks a model.", phases: ["output"], actions: ["block"], deterministic: false,
      config_schema: schema({ model: { type: "object", properties: { provider: { type: "string" }, name: { type: "string" } } } }, ["model"]) },
  ],
  limits: { max_processors_per_phase: 8, max_regex_patterns: 20, max_regex_pattern_length: 200, max_test_sample_bytes: 32768 },
  moderation_categories: ["hate"],
};

const RUN = "11111111-2222-3333-4444-555555555555";
const row = (over: Record<string, unknown> = {}) => ({
  run_id: RUN, seq: 1, ts: "2026-11-13T10:00:00Z", agent_id: "agent-1", agent_version: 1, parent_run_id: null,
  phase: "input", index: 0, processor_type: "pii_redact", action: "redact", match_counts: { email: 1 }, ...over,
});
const agent = (n: number) => ({
  id: `agent-${n}`, name: `Agent ${n}`, latest_version: 1,
  draft_spec: { spec_version: 1, name: `Agent ${n}`, instructions: "", model: { provider: "test", name: "m" }, tools: [] },
});

type Res = { status: number; body: unknown };
type Handler = (url: URL, init?: RequestInit) => Res | Promise<Res>;
let calls: { url: URL; init?: RequestInit }[] = [];
let handlers: Record<string, Handler> = {};
let agentsBody: unknown[] = [];
let versionSpec: Record<string, unknown> = {};

function installFetch() {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input);
    calls.push({ url, init });
    const path = url.pathname.replace("/api/v1", "");
    let res: Res = { status: 404, body: { detail: "not found" } };
    if (handlers[path]) res = await handlers[path](url, init);
    else if (path === "/processors") res = { status: 200, body: CATALOG };
    else if (path === "/agents") res = { status: 200, body: agentsBody };
    else if (path === "/processor-results") res = { status: 200, body: { items: [], next_before: null } };
    else if (/^\/agents\/[^/]+\/versions\/\d+$/.test(path)) res = { status: 200, body: { spec: versionSpec } };
    return new Response(JSON.stringify(res.body), { status: res.status, headers: { "Content-Type": "application/json" } });
  }));
}
const resultCalls = () => calls.filter((c) => c.url.pathname.endsWith("/processor-results"));
const Detail = ({ type = "pii_redact" }: { type?: string }) => <ProcessorDetail type={type} />;
const rowOf = (rows: HTMLElement[], id: string) => rows.find((r) => r.dataset.rowId === id)!;

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  agentsBody = [agent(1)];
  versionSpec = {};
  handlers = {};
  installFetch();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  document.documentElement.classList.remove("dark");
});

describe("processors list", () => {
  it("shows outcome counts per type: exact, N+ at a full page, and a dash for a failing type", async () => {
    handlers["/processor-results"] = (url) => {
      const type = url.searchParams.get("type");
      if (type === "pii_redact") return { status: 200, body: { items: [row({ seq: 1 }), row({ seq: 2 }), row({ seq: 3 })], next_before: "cursor" } };
      if (type === "regex_blocklist") return { status: 500, body: { detail: "boom" } };
      if (type === "max_length") return { status: 200, body: { items: [row({ processor_type: "max_length" })], next_before: null } };
      return { status: 200, body: { items: [], next_before: null } };
    };
    render(<ProcessorsPage />);
    const rows = await screen.findAllByTestId("processor-row");
    const cell = (id: string) => within(rowOf(rows, id)).getByTestId("processor-row-outcomes");
    await waitFor(() => expect(cell("pii_redact")).toHaveTextContent("3+"));
    expect(cell("regex_blocklist")).toHaveTextContent("—");
    expect(cell("max_length")).toHaveTextContent("1");
    expect(cell("max_length")).not.toHaveTextContent("+");
    expect(cell("moderation")).toHaveTextContent("0");
    expect(resultCalls().every((c) => c.url.searchParams.get("limit") === "200")).toBe(true);
  });

  it("makes one outcome call per type and no refetch storm", async () => {
    render(<ProcessorsPage />);
    const rows = await screen.findAllByTestId("processor-row");
    await waitFor(() => expect(within(rowOf(rows, "moderation")).getByTestId("processor-row-outcomes")).toHaveTextContent("0"));
    await new Promise((r) => setTimeout(r, 50));
    expect(resultCalls()).toHaveLength(4);
    expect(calls.filter((c) => c.url.pathname.endsWith("/processors"))).toHaveLength(1);
    expect(calls.filter((c) => c.url.pathname.endsWith("/agents"))).toHaveLength(1);
  });

  it("counts agents per type and notes unreadable versions as unknown", async () => {
    agentsBody = [agent(1), agent(2), agent(3)];
    handlers["/agents/agent-1/versions/1"] = () => ({ status: 200, body: { spec: { input_processors: [{ type: "pii_redact", config: {} }] } } });
    handlers["/agents/agent-2/versions/1"] = () => ({ status: 200, body: { spec: { output_processors: [{ type: "pii_redact", config: {} }, { type: "moderation", config: {} }] } } });
    handlers["/agents/agent-3/versions/1"] = () => ({ status: 500, body: { detail: "boom" } });
    render(<ProcessorsPage />);
    const rows = await screen.findAllByTestId("processor-row");
    const usage = (id: string) => within(rowOf(rows, id)).getByTestId("processor-row-usage");
    await waitFor(() => expect(usage("pii_redact")).toHaveTextContent("2 agents (1 unknown)"));
    expect(usage("moderation")).toHaveTextContent("1 agent (1 unknown)");
    expect(usage("regex_blocklist")).toHaveTextContent("0 agents (1 unknown)");
  });

  it("shows a usage error and keeps the rows and counts", async () => {
    handlers["/agents"] = () => ({ status: 500, body: { detail: "agents are down" } });
    render(<ProcessorsPage />);
    expect(await screen.findByTestId("usage-error")).toHaveTextContent("agents are down");
    const rows = await screen.findAllByTestId("processor-row");
    expect(rows).toHaveLength(4);
    await waitFor(() => expect(within(rows[0]).getByTestId("processor-row-outcomes")).toHaveTextContent("0"));
    expect(within(rows[0]).queryByTestId("processor-row-usage")).toBeNull();
  });

  it("shows the empty and error states without rows and without outcome calls", async () => {
    handlers["/processors"] = () => ({ status: 200, body: { ...CATALOG, items: [] } });
    const empty = render(<ProcessorsPage />);
    expect(await screen.findByTestId("catalog-empty")).toBeInTheDocument();
    expect(screen.queryAllByTestId("processor-row")).toHaveLength(0);
    empty.unmount();
    handlers["/processors"] = () => ({ status: 500, body: { detail: "catalog down" } });
    render(<ProcessorsPage />);
    expect(await screen.findByTestId("catalog-error")).toHaveTextContent("catalog down");
    expect(screen.queryByTestId("processor-list")).toBeNull();
    expect(resultCalls()).toHaveLength(0);
  });

  it("searches by title, id and description", async () => {
    render(<ProcessorsPage />);
    await screen.findAllByTestId("processor-row");
    const box = screen.getByRole("searchbox");
    fireEvent.change(box, { target: { value: "personal data" } });
    await waitFor(() => expect(screen.getAllByTestId("processor-row")).toHaveLength(1));
    expect(screen.getByTestId("processor-row")).toHaveAttribute("data-row-id", "pii_redact");
    fireEvent.change(box, { target: { value: "regex_" } });
    await waitFor(() => expect(screen.getByTestId("processor-row")).toHaveAttribute("data-row-id", "regex_blocklist"));
    fireEvent.change(box, { target: { value: "zzz" } });
    expect(await screen.findByTestId("datalist-no-match")).toBeInTheDocument();
  });

  it("sorts by the Type and Outcomes columns", async () => {
    handlers["/processor-results"] = (url) => ({
      status: 200,
      body: { items: url.searchParams.get("type") === "moderation" ? [row(), row({ seq: 2 })] : [], next_before: null },
    });
    render(<ProcessorsPage />);
    const ids = () => screen.getAllByTestId("processor-row").map((r) => r.dataset.rowId);
    const rows = await screen.findAllByTestId("processor-row");
    await waitFor(() => expect(within(rowOf(rows, "moderation")).getByTestId("processor-row-outcomes")).toHaveTextContent("2"));
    fireEvent.click(within(screen.getByRole("columnheader", { name: /^Type/ })).getByRole("button"));
    expect(ids()).toEqual(["max_length", "moderation", "pii_redact", "regex_blocklist"]);
    fireEvent.click(within(screen.getByRole("columnheader", { name: /Outcomes/ })).getByRole("button"));
    expect(ids().at(-1)).toBe("moderation");
    expect(screen.getByRole("columnheader", { name: /Outcomes/ })).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(within(screen.getByRole("columnheader", { name: /Outcomes/ })).getByRole("button"));
    expect(screen.getByRole("columnheader", { name: /Outcomes/ })).toHaveAttribute("aria-sort", "descending");
    expect(ids()[0]).toBe("moderation");
  });

  it("opens a row with Enter and links list to detail", async () => {
    render(<ProcessorsPage />);
    const rows = await screen.findAllByTestId("processor-row");
    for (const id of ["pii_redact", "regex_blocklist", "max_length", "moderation"]) {
      const title = CATALOG.items.find((i) => i.id === id)!.title;
      expect(within(rowOf(rows, id)).getByRole("link", { name: title })).toHaveAttribute("href", routes.processor(id));
    }
    const target = rowOf(rows, "max_length");
    const link = within(target).getByRole("link", { name: "Maximum length" });
    const onClick = vi.fn((event: MouseEvent) => event.preventDefault());
    link.addEventListener("click", onClick);
    fireEvent.keyDown(target, { key: "Enter" });
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe("processor detail: every catalog type", () => {
  it.each(CATALOG.items)("renders $id with title, overview, config fields and its console state", async (item) => {
    render(<Detail type={item.id} />);
    const card = await screen.findByTestId("processor-card");
    expect(card).toHaveAttribute("data-processor-type", item.id);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(item.title);
    expect(within(card).getByText(item.description)).toBeInTheDocument();
    const fields = within(within(card).getByTestId("processor-config-fields")).getAllByRole("listitem");
    expect(fields.map((li) => li.querySelector(".font-mono")?.textContent)).toEqual(Object.keys(item.config_schema.properties));
    expect(within(card).getByTestId("processor-scope-note")).toHaveTextContent("Tool results and request context values are not processed");
    if (item.deterministic) {
      expect(screen.getByRole("form", { name: "Processor test console" })).toBeInTheDocument();
      expect(screen.queryByTestId("console-unavailable")).toBeNull();
    } else {
      expect(screen.getByTestId("console-unavailable")).toHaveTextContent("Test it in the playground");
      expect(screen.queryByTestId("console-layout")).toBeNull();
    }
    await waitFor(() => expect(resultCalls().length).toBeGreaterThan(0));
    for (const call of resultCalls()) expect(call.url.searchParams.get("type")).toBe(item.id);
  });

  it("the console starts on the page's own type", async () => {
    render(<Detail type="max_length" />);
    const select = await screen.findByLabelText("Processor 1");
    expect(select).toHaveValue("max_length");
  });

  it("states the RE2 limits exactly for the regex blocklist and only the phase limit elsewhere", async () => {
    const regex = render(<Detail type="regex_blocklist" />);
    const limits = await screen.findByTestId("processor-limits");
    expect(limits.textContent).toBe(
      "At most 8 processors per phase."
      + "RE2 syntax (linear time; no backreferences or lookaround), at most 20 patterns of 200 characters.",
    );
    regex.unmount();
    render(<Detail type="pii_redact" />);
    expect((await screen.findByTestId("processor-limits")).textContent).toBe("At most 8 processors per phase.");
  });
});

describe("processor detail: console round-trip", () => {
  const SECRET = "hunter2-secret@example.com";
  const respond = (body: unknown) => {
    handlers["/processors/test"] = () => ({ status: 200, body });
  };
  const sent = () => JSON.parse(String(calls.filter((c) => c.url.pathname.endsWith("/processors/test")).at(-1)!.init?.body));
  const fill = (text: string) => fireEvent.change(screen.getByLabelText("Sample text"), { target: { value: text } });
  // These prove the UI does not echo the sample back (the mocked API never returns it); that the
  // server stores and serves no matched text is covered by the P0.9 API tests.
  const expectSampleNotEchoed = () => {
    const panel = screen.getByTestId("console-result");
    expect(panel.textContent).not.toContain(SECRET);
    expect(panel.textContent).not.toContain("hunter2");
    expect(screen.getByTestId("console-json").textContent).not.toContain("hunter2");
  };

  it("pii_redact: sends the type, config and sample; shows counts and the redacted text only", async () => {
    respond({ blocked: false, text: "Reach me at [REDACTED]", results: [{ index: 0, processor_type: "pii_redact", action: "redact", match_counts: { email: 1 } }] });
    render(<Detail type="pii_redact" />);
    expect(await screen.findByTestId("console-result-empty")).toBeInTheDocument();
    fill(`Reach me at ${SECRET}`);
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    expect(await screen.findByTestId("console-result")).toHaveTextContent("Passed.");
    expect(sent()).toEqual({ processors: [{ type: "pii_redact", config: { kinds: ["email"], action: "redact" } }], text: `Reach me at ${SECRET}` });
    expect(screen.getByTestId("console-outcome")).toHaveTextContent("pii_redact");
    expect(screen.getByTestId("console-outcome")).toHaveTextContent("email");
    expect(screen.getByTestId("console-output")).toHaveTextContent("Reach me at [REDACTED]");
    expectSampleNotEchoed();
  });

  it("regex_blocklist: a block shows no processed text and the UI does not echo the sample", async () => {
    respond({ blocked: true, text: null, results: [{ index: 0, processor_type: "regex_blocklist", action: "block", match_counts: { pattern_0: 1 } }] });
    render(<Detail type="regex_blocklist" />);
    fireEvent.change(await screen.findByLabelText(/Patterns/), { target: { value: "hunter2\n" } });
    fill(`my password is ${SECRET}`);
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    expect(await screen.findByTestId("console-result")).toHaveTextContent("Blocked");
    expect(sent().processors).toEqual([{ type: "regex_blocklist", config: { patterns: ["hunter2"], action: "block" } }]);
    expect(screen.queryByTestId("console-output")).toBeNull();
    expectSampleNotEchoed();
  });

  it("max_length: truncate chosen in the form is sent, and the cut text is shown", async () => {
    respond({ blocked: false, text: "hunte", results: [{ index: 0, processor_type: "max_length", action: "truncate", match_counts: {}, reason: "over 5 characters" }] });
    render(<Detail type="max_length" />);
    await screen.findByTestId("console-result-empty");
    const consoleForm = within(screen.getByRole("form", { name: "Processor test console" }));
    fireEvent.change(consoleForm.getByLabelText("Action"), { target: { value: "truncate" } });
    fill(SECRET);
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    expect(await screen.findByTestId("console-outcome")).toHaveTextContent("over 5 characters");
    expect(sent().processors[0]).toEqual({ type: "max_length", config: { max_chars: 5, action: "truncate" } });
    expect(screen.getByTestId("console-status")).toHaveTextContent("Passed. Truncated.");
    expect(screen.getByTestId("console-output")).toHaveTextContent("hunte");
    expectSampleNotEchoed();
  });
});

describe("processor detail: fixed-type results", () => {
  const lastParams = () => Object.fromEntries(resultCalls().at(-1)!.url.searchParams);

  it("has no Processor filter and sends the type on every request, including Reset and paging", async () => {
    handlers["/processor-results"] = (url) => ({
      status: 200,
      body: url.searchParams.get("before") === "c1"
        ? { items: [row({ seq: 1 })], next_before: null }
        : { items: [row({ seq: 2 })], next_before: "c1" },
    });
    render(<Detail type="max_length" />);
    const form = within(await screen.findByRole("form", { name: "Filter outcomes" }));
    expect(form.queryByLabelText("Processor")).toBeNull();
    expect(form.queryByRole("option", { name: "All types" })).toBeNull();
    await screen.findByTestId("result-row");
    fireEvent.change(form.getByLabelText("Phase"), { target: { value: "output" } });
    fireEvent.click(form.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(lastParams()).toMatchObject({ type: "max_length", phase: "output" }));
    fireEvent.click(await screen.findByRole("button", { name: "Load older outcomes" }));
    await waitFor(() => expect(screen.getAllByTestId("result-row")).toHaveLength(2));
    expect(lastParams()).toMatchObject({ type: "max_length", phase: "output", before: "c1" });
    fireEvent.click(form.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(lastParams()).toEqual({ type: "max_length", limit: "50", from: expect.any(String) }));
    expect(resultCalls().every((c) => c.url.searchParams.get("type") === "max_length")).toBe(true);
  });

  it("links each outcome's run to the trace route and shows the empty state", async () => {
    const other = "99999999-0000-0000-0000-000000000000";
    handlers["/processor-results"] = () => ({ status: 200, body: { items: [row({ seq: 1 }), row({ seq: 2, run_id: other })], next_before: null } });
    const view = render(<Detail />);
    const rows = await screen.findAllByTestId("result-row");
    expect(within(rows[0]).getByRole("link", { name: `Open trace of run ${RUN}` })).toHaveAttribute("href", routes.trace(RUN));
    expect(within(rows[1]).getByRole("link", { name: `Open trace of run ${other}` })).toHaveAttribute("href", routes.trace(other));
    expect(within(rows[0]).getByRole("link", { name: "Agent 1" })).toHaveAttribute("href", routes.agentEditor("agent-1"));
    view.unmount();
    handlers["/processor-results"] = () => ({ status: 200, body: { items: [], next_before: null } });
    render(<Detail />);
    expect(await screen.findByTestId("results-empty")).toBeInTheDocument();
  });

  it("links the usage list to the agent", async () => {
    versionSpec = { input_processors: [{ type: "pii_redact", config: {} }] };
    render(<Detail />);
    const usage = await screen.findByTestId("processor-usage");
    expect(within(usage).getByRole("link", { name: "Agent 1" })).toHaveAttribute("href", routes.agentEditor("agent-1"));
  });
});

describe("processor detail: unknown type, layout and a11y", () => {
  it("an unknown type is not found with a way back and no console, results or outcome calls", async () => {
    render(<Detail type="nope" />);
    const missing = await screen.findByTestId("processor-not-found");
    expect(missing).toHaveTextContent("nope");
    expect(within(missing).getByRole("link", { name: "All processors" })).toHaveAttribute("href", routes.processors());
    expect(screen.queryByTestId("processor-card")).toBeNull();
    expect(screen.queryByTestId("console-layout")).toBeNull();
    expect(screen.queryByRole("form", { name: "Filter outcomes" })).toBeNull();
    expect(resultCalls()).toHaveLength(0);
  });

  it("stacks the console below lg and uses the 22rem column from lg", async () => {
    render(<Detail />);
    const layout = await screen.findByTestId("console-layout");
    const classes = layout.className.split(/\s+/);
    expect(classes).toContain("grid");
    expect(classes).not.toContain("grid-cols-[22rem_minmax(0,1fr)]");
    expect(classes).toContain("lg:grid-cols-[22rem_minmax(0,1fr)]");
    expect(classes.filter((c) => /^(sm|md):grid-cols/.test(c))).toEqual([]);
    expect(layout.children).toHaveLength(2);
    expect(layout.firstElementChild!.tagName).toBe("FORM");
  });

  it.each(["light", "dark"])("has no axe violations in the %s theme (list and detail)", async (theme) => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    handlers["/processor-results"] = () => ({ status: 200, body: { items: [row()], next_before: "c" } });
    versionSpec = { input_processors: [{ type: "pii_redact", config: {} }] };
    const list = render(<ProcessorsPage />);
    await screen.findAllByTestId("processor-row");
    await waitFor(() => expect(within(screen.getAllByTestId("processor-row")[0]).getByTestId("processor-row-usage")).toBeInTheDocument());
    await expectNoAxeViolations(list.container);
    list.unmount();
    const detail = render(<Detail />);
    await screen.findByTestId("result-row");
    await screen.findByTestId("processor-usage");
    await expectNoAxeViolations(detail.container);
    detail.unmount();
    const unavailable = render(<Detail type="moderation" />);
    await screen.findByTestId("console-unavailable");
    await expectNoAxeViolations(unavailable.container);
    unavailable.unmount();
    const missing = render(<Detail type="nope" />);
    await screen.findByTestId("processor-not-found");
    await expectNoAxeViolations(missing.container);
  });
});
