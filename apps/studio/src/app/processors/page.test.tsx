import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ProcessorsPage from "./page";
import { ProcessorDetail } from "@/components/processors/ProcessorDetail";
import { OUTCOME_COUNT_LIMIT, ProcessorsDataProvider } from "@/components/processors/processorData";
import { consoleVerdict } from "@/components/processors/ProcessorTestConsole";
import { api, ApiError, type Agent } from "@/lib/api";
import { loadAgentProcessorSpecs, processors, type ProcessorCatalog, type ProcessorResultItem } from "@/lib/processors";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations } from "@/test/a11y";

vi.mock("@/lib/processors", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/processors")>();
  return {
    ...actual,
    processors: { catalog: vi.fn(), test: vi.fn(), results: vi.fn() },
    loadAgentProcessorSpecs: vi.fn(),
  };
});
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, agents: { ...actual.api.agents, list: vi.fn() } } };
});
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a>,
}));

const catalog: ProcessorCatalog = {
  items: [
    {
      id: "pii_redact", title: "PII redaction", description: "Detects personal data.", phases: ["input", "output"], actions: ["redact", "block"], deterministic: true,
      config_schema: {
        type: "object",
        properties: {
          kinds: { type: "array", default: ["email", "phone"], description: "Kinds of personal data to detect", items: { type: "string", enum: ["email", "phone"] } },
          action: { type: "string", default: "redact", enum: ["redact", "block"] },
        },
      },
    },
    {
      id: "max_length", title: "Maximum length", description: "Blocks long text.", phases: ["input", "output"], actions: ["block", "truncate"], deterministic: true,
      config_schema: { type: "object", required: ["max_chars"], properties: { max_chars: { type: "integer", minimum: 1 }, action: { type: "string", default: "block", enum: ["block", "truncate"] } } },
    },
    {
      id: "moderation", title: "Moderation", description: "Asks a model.", phases: ["input", "output"], actions: ["block"], deterministic: false,
      config_schema: { type: "object", required: ["model"], properties: { model: { type: "object", properties: { provider: { type: "string" }, name: { type: "string" } } } } },
    },
  ],
  limits: { max_processors_per_phase: 8, max_regex_patterns: 20, max_regex_pattern_length: 200, max_test_sample_bytes: 32768 },
  moderation_categories: ["hate", "violence"],
};

const agents: Agent[] = [
  { id: "agent-1", name: "Support bot", latest_version: 2, draft_spec: { spec_version: 1, name: "Support bot", instructions: "", model: { provider: "test", name: "m" }, tools: [] } },
];

const RUN_ID = "11111111-2222-3333-4444-555555555555";
const blocked: ProcessorResultItem = {
  run_id: RUN_ID, seq: 4, ts: "2026-11-13T10:00:00Z", agent_id: "agent-1", agent_version: 2, parent_run_id: null,
  phase: "input", index: 0, processor_type: "pii_redact", action: "block", match_counts: { email: 2 },
};

const Detail = ({ type = "pii_redact" }: { type?: string }) => <ProcessorDetail type={type} />;

describe("Processors list and detail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(processors.catalog).mockResolvedValue(catalog);
    vi.mocked(processors.results).mockResolvedValue({ items: [blocked], next_before: null });
    vi.mocked(api.agents.list).mockResolvedValue(agents);
    vi.mocked(loadAgentProcessorSpecs).mockResolvedValue([
      { agent: agents[0], draft: null, latest: { input_processors: [{ type: "pii_redact", config: {} }] } },
    ]);
  });

  it("lists the catalog as rows linking to each type, with usage and 24 h outcome counts", async () => {
    render(<ProcessorsPage />);
    const rows = await screen.findAllByTestId("processor-row");
    expect(rows).toHaveLength(3);
    const pii = rows.find((row) => row.dataset.rowId === "pii_redact")!;
    expect(within(pii).getByRole("link", { name: "PII redaction" })).toHaveAttribute("href", routes.processor("pii_redact"));
    expect(await within(pii).findByText("1 agent")).toBeInTheDocument();
    await waitFor(() => expect(within(pii).getByTestId("processor-row-outcomes")).toHaveTextContent("1"));
    const moderation = rows.find((row) => row.dataset.rowId === "moderation")!;
    expect(within(moderation).getByTestId("processor-row-usage")).toHaveTextContent("0 agents");
    expect(processors.results).toHaveBeenCalledWith(expect.objectContaining({ type: "pii_redact", limit: OUTCOME_COUNT_LIMIT, from: expect.any(String) }), expect.any(AbortSignal));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Processors");
  });

  it("marks a count with '+' when a type's 24 h page is full", async () => {
    vi.mocked(processors.results).mockResolvedValue({ items: [blocked], next_before: "more" });
    render(<ProcessorsPage />);
    const rows = await screen.findAllByTestId("processor-row");
    await waitFor(() => expect(within(rows[0]).getByTestId("processor-row-outcomes")).toHaveTextContent("1+"));
  });

  it("renders the type's overview: config schema, limits, scope note and the agents using it", async () => {
    render(<Detail />);
    const card = await screen.findByTestId("processor-card");
    expect(card).toHaveAttribute("data-processor-type", "pii_redact");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("PII redaction");
    expect(within(card).getByTestId("processor-config-fields")).toHaveTextContent("kinds");
    expect(within(card).getByTestId("processor-scope-note")).toHaveTextContent("Tool results and request context values are not processed");
    const usage = await screen.findByTestId("processor-usage");
    expect(within(usage).getByRole("link", { name: "Support bot" })).toHaveAttribute("href", routes.agentEditor("agent-1"));
    expect(usage).toHaveTextContent("v2");
  });

  it("a model-calling type has no console, only 'test in the playground'", async () => {
    render(<Detail type="moderation" />);
    const card = await screen.findByTestId("processor-card");
    expect(within(card).getByText(/test in the playground/)).toBeInTheDocument();
    expect(screen.getByTestId("console-unavailable")).toHaveTextContent("Test it in the playground");
    expect(screen.queryByRole("form", { name: "Processor test console" })).not.toBeInTheDocument();
    expect(await screen.findByTestId("processor-usage-empty")).toBeInTheDocument();
  });

  it("an unknown type is not found", async () => {
    render(<Detail type="nope" />);
    expect(await screen.findByTestId("processor-not-found")).toHaveTextContent("Processor type not found");
    expect(screen.getByRole("link", { name: "All processors" })).toHaveAttribute("href", routes.processors());
    expect(screen.queryByTestId("processor-card")).not.toBeInTheDocument();
  });

  it("puts the console form in a 22rem column beside the result, stacked below 1024 px", async () => {
    render(<Detail />);
    const layout = await screen.findByTestId("console-layout");
    expect(layout.className).toContain("grid");
    expect(layout.className).toContain("lg:grid-cols-[22rem_minmax(0,1fr)]");
    expect(layout.className).not.toMatch(/(^|\s)grid-cols-/);
    expect(screen.getByTestId("console-result-empty")).toBeInTheDocument();
  });

  it("lists recent outcomes of the type with counts only and a link to the run", async () => {
    render(<Detail />);
    const row = await screen.findByTestId("result-row");
    expect(within(row).getByRole("link", { name: `Open trace of run ${RUN_ID}` })).toHaveAttribute("href", routes.trace(RUN_ID));
    expect(row).toHaveTextContent("email ×2");
    expect(row).toHaveTextContent("block");
    expect(row).toHaveTextContent("input");
    expect(screen.getByText(/Matched text is never stored/)).toBeInTheDocument();
    expect(processors.results).toHaveBeenCalledWith(expect.objectContaining({ type: "pii_redact", limit: 50 }));
  });

  it("applies filters and validates the run id", async () => {
    render(<Detail />);
    await screen.findByTestId("result-row");
    await screen.findByRole("option", { name: "Support bot" });
    const filters = within(screen.getByRole("form", { name: "Filter outcomes" }));
    expect(filters.queryByLabelText("Processor")).not.toBeInTheDocument();

    fireEvent.change(filters.getByLabelText("Run ID"), { target: { value: "not-a-uuid" } });
    fireEvent.click(filters.getByRole("button", { name: "Apply" }));
    expect(await screen.findByTestId("results-filter-error")).toHaveTextContent("Run ID must be a full run UUID");

    vi.mocked(processors.results).mockResolvedValue({ items: [], next_before: null });
    fireEvent.change(filters.getByLabelText("Run ID"), { target: { value: RUN_ID } });
    fireEvent.change(filters.getByLabelText("Agent"), { target: { value: "agent-1" } });
    fireEvent.change(filters.getByLabelText("Action"), { target: { value: "block" } });
    fireEvent.change(filters.getByLabelText("Phase"), { target: { value: "input" } });
    fireEvent.click(filters.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(processors.results).toHaveBeenLastCalledWith(expect.objectContaining({
      agent_id: "agent-1", type: "pii_redact", action: "block", phase: "input", run_id: RUN_ID, limit: 50,
    })));
    expect(await screen.findByTestId("results-empty")).toHaveTextContent("No outcomes match these filters");
  });

  it("pages older outcomes with next_before", async () => {
    vi.mocked(processors.results)
      .mockResolvedValueOnce({ items: [blocked], next_before: "cursor-1" })
      .mockResolvedValueOnce({ items: [{ ...blocked, seq: 2, action: "redact" }], next_before: null });
    render(<Detail />);
    fireEvent.click(await screen.findByRole("button", { name: "Load older outcomes" }));
    await waitFor(() => expect(screen.getAllByTestId("result-row")).toHaveLength(2));
    expect(processors.results).toHaveBeenLastCalledWith(expect.objectContaining({ before: "cursor-1" }));
    expect(screen.queryByRole("button", { name: "Load older outcomes" })).not.toBeInTheDocument();
  });

  it("shows the empty state when there are no outcomes", async () => {
    vi.mocked(processors.results).mockResolvedValue({ items: [], next_before: null });
    render(<Detail />);
    expect(await screen.findByTestId("results-empty")).toHaveTextContent("No outcomes in the last 24 hours");
  });

  it("runs the test console and shows outcomes and processed text", async () => {
    vi.mocked(processors.test).mockResolvedValue({
      text: "mail [REDACTED:email]", blocked: false,
      results: [{ index: 0, processor_type: "pii_redact", action: "redact", match_counts: { email: 1 } }],
    });
    render(<Detail />);
    const sample = await screen.findByLabelText("Sample text");
    fireEvent.change(sample, { target: { value: "mail a@b.co" } });
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    await waitFor(() => expect(processors.test).toHaveBeenCalledWith(
      [{ type: "pii_redact", config: { kinds: ["email", "phone"], action: "redact" } }],
      "mail a@b.co",
    ));
    const result = await screen.findByTestId("console-result");
    expect(within(result).getByTestId("console-outcome")).toHaveTextContent("email ×1");
    expect(within(result).getByTestId("console-output")).toHaveTextContent("mail [REDACTED:email]");
  });

  it("chains processors and reports a block without processed text", async () => {
    vi.mocked(processors.test).mockResolvedValue({
      text: null, blocked: true,
      results: [{ index: 0, processor_type: "max_length", action: "block", match_counts: {}, reason: "too_long" }],
    });
    render(<Detail type="max_length" />);
    fireEvent.change(await screen.findByLabelText("Max chars *"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Add processor" }));
    expect(screen.getAllByTestId("console-step")).toHaveLength(2);
    fireEvent.change(screen.getByLabelText("Sample text"), { target: { value: "too long" } });
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    await waitFor(() => expect(processors.test).toHaveBeenCalledWith(
      [{ type: "max_length", config: { max_chars: 3, action: "block" } }, { type: "pii_redact", config: { kinds: ["email", "phone"], action: "redact" } }],
      "too long",
    ));
    const result = await screen.findByTestId("console-result");
    expect(result).toHaveTextContent("Blocked");
    expect(within(result).queryByTestId("console-output")).not.toBeInTheDocument();
  });

  it("offers moderation only as 'test in the playground' and maps the API refusal", async () => {
    vi.mocked(processors.test).mockRejectedValue(new ApiError(422, "moderation calls a model and cannot run in the test console; test it in the playground"));
    render(<Detail />);
    const option = await screen.findByRole("option", { name: "Moderation (test in the playground)" });
    expect(option).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    expect(await screen.findByTestId("console-error")).toHaveTextContent("Test it in the playground");
  });

  it("shows an error when the catalog cannot be loaded", async () => {
    vi.mocked(processors.catalog).mockRejectedValue(new ApiError(500, "HTTP 500"));
    render(<Detail />);
    expect(await screen.findByTestId("catalog-error")).toHaveTextContent("HTTP 500");
    expect(screen.getByText("The test console needs the processor catalog.")).toBeInTheDocument();
  });

  it("the list shows the catalog error and no rows", async () => {
    vi.mocked(processors.catalog).mockRejectedValue(new ApiError(500, "HTTP 500"));
    render(<ProcessorsPage />);
    expect(await screen.findByTestId("catalog-error")).toHaveTextContent("HTTP 500");
    expect(screen.queryByTestId("processor-row")).not.toBeInTheDocument();
  });

  it("marks agents whose latest version could not be read as unknown", async () => {
    vi.mocked(loadAgentProcessorSpecs).mockResolvedValue([{ agent: agents[0], draft: null, latest: null, failed: true }]);
    render(<Detail />);
    expect(await screen.findByTestId("processor-usage-unknown")).toHaveTextContent("unknown: v2 could not be read");
    expect(screen.queryByTestId("processor-usage-empty")).not.toBeInTheDocument();
  });

  it("shows a 422 message with its field location", async () => {
    vi.mocked(processors.test).mockRejectedValue(new ApiError(422, "processors.0.config.max_chars: Input should be greater than or equal to 1"));
    render(<Detail />);
    fireEvent.click(await screen.findByRole("button", { name: "Run test" }));
    expect(await screen.findByTestId("console-error")).toHaveTextContent("processors.0.config.max_chars: Input should be greater than or equal to 1");
  });

  it("clears the previous result when the sample or the steps change", async () => {
    vi.mocked(processors.test).mockResolvedValue({ text: "ok", blocked: false, results: [{ index: 0, processor_type: "pii_redact", action: "allow", match_counts: {} }] });
    render(<Detail />);
    const sample = await screen.findByLabelText("Sample text");
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    await screen.findByTestId("console-result");
    fireEvent.change(sample, { target: { value: "changed" } });
    expect(screen.queryByTestId("console-result")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    await screen.findByTestId("console-result");
    fireEvent.click(screen.getByRole("button", { name: "Add processor" }));
    expect(screen.queryByTestId("console-result")).not.toBeInTheDocument();
  });

  it("drops an answer for a sample that changed while the test ran", async () => {
    let resolve: (value: Awaited<ReturnType<typeof processors.test>>) => void = () => {};
    vi.mocked(processors.test).mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<Detail />);
    const sample = await screen.findByLabelText("Sample text");
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    fireEvent.change(sample, { target: { value: "newer" } });
    resolve({ text: "stale", blocked: false, results: [] });
    await waitFor(() => expect(screen.getByRole("button", { name: "Run test" })).not.toBeDisabled());
    expect(screen.queryByTestId("console-result")).not.toBeInTheDocument();
  });

  it("keeps a typed Enter in the pattern list and sends only non-empty patterns", async () => {
    const withBlocklist: ProcessorCatalog = {
      ...catalog,
      items: [{
        id: "regex_blocklist", title: "Regex blocklist", description: "Blocks patterns.", phases: ["input", "output"], actions: ["block", "redact"], deterministic: true,
        config_schema: { type: "object", required: ["patterns"], properties: { patterns: { type: "array", items: { type: "string" } }, action: { type: "string", default: "block", enum: ["block", "redact"] } } },
      }],
    };
    vi.mocked(processors.catalog).mockResolvedValue(withBlocklist);
    vi.mocked(processors.test).mockResolvedValue({ text: "x", blocked: false, results: [] });
    render(<Detail type="regex_blocklist" />);
    expect(await screen.findByTestId("processor-limits")).toHaveTextContent("RE2 syntax");
    expect(screen.getByTestId("processor-limits")).toHaveTextContent("at most 20 patterns of 200 characters");
    const patterns = await screen.findByLabelText("Patterns *");
    fireEvent.change(patterns, { target: { value: "secret\n" } });
    expect(patterns).toHaveValue("secret\n");
    fireEvent.change(patterns, { target: { value: "secret\n\nFLAG_ME" } });
    expect(patterns).toHaveValue("secret\n\nFLAG_ME");
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    await waitFor(() => expect(processors.test).toHaveBeenCalledWith(
      [{ type: "regex_blocklist", config: { patterns: ["secret", "FLAG_ME"], action: "block" } }],
      "",
    ));
  });

  it("has no axe violations on the list or the detail page", async () => {
    const list = render(<ProcessorsPage />);
    await screen.findAllByTestId("processor-row");
    await expectNoAxeViolations(list.container);
    list.unmount();
    const detail = render(<Detail />);
    await screen.findByTestId("result-row");
    await screen.findByTestId("processor-usage");
    await expectNoAxeViolations(detail.container);
  });

  it("shares the catalog and Used by scan across pages under the provider", async () => {
    const view = render(<ProcessorsDataProvider><ProcessorsPage /></ProcessorsDataProvider>);
    await screen.findAllByTestId("processor-row");
    await waitFor(() => expect(loadAgentProcessorSpecs).toHaveBeenCalledTimes(1));
    view.rerender(<ProcessorsDataProvider><Detail /></ProcessorsDataProvider>);
    expect(await screen.findByTestId("processor-usage")).toHaveTextContent("Support bot");
    expect(processors.catalog).toHaveBeenCalledTimes(1);
    expect(api.agents.list).toHaveBeenCalledTimes(1);
    expect(loadAgentProcessorSpecs).toHaveBeenCalledTimes(1);
  });

  it("aborts the outcome-count requests when the list unmounts", async () => {
    const signals: AbortSignal[] = [];
    vi.mocked(processors.results).mockImplementation((_filters, signal) => {
      if (signal) signals.push(signal);
      return new Promise(() => {});
    });
    const view = render(<ProcessorsPage />);
    await waitFor(() => expect(signals).toHaveLength(3));
    expect(signals.every((signal) => !signal.aborted)).toBe(true);
    view.unmount();
    expect(signals.every((signal) => signal.aborted)).toBe(true);
  });

  it("starts the detail outcomes at the last 24 hours, says so, and offers All time", async () => {
    const before = Date.now();
    render(<Detail />);
    await screen.findByTestId("result-row");
    const first = vi.mocked(processors.results).mock.calls[0][0]!;
    const from = Date.parse(first.from!);
    expect(before - from).toBeGreaterThanOrEqual(24 * 3_600_000 - 1000);
    expect(before - from).toBeLessThan(24 * 3_600_000 + 60_000);
    expect(screen.getByTestId("results-range")).toHaveTextContent("Showing the last 24 hours");
    expect(screen.getByLabelText("From")).not.toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "All time" }));
    await waitFor(() => expect(screen.getByTestId("results-range")).toHaveTextContent("Showing all time."));
    expect(vi.mocked(processors.results).mock.calls.at(-1)![0]).not.toHaveProperty("from", expect.any(String));
    expect(screen.getByLabelText("From")).toHaveValue("");
    expect(screen.queryByRole("button", { name: "All time" })).not.toBeInTheDocument();
  });

  it("announces only a one-line verdict", async () => {
    vi.mocked(processors.test).mockResolvedValue({
      text: "mail [REDACTED:email]", blocked: false,
      results: [{ index: 0, processor_type: "pii_redact", action: "redact", match_counts: { email: 2 } }],
    });
    render(<Detail />);
    const status = await screen.findByTestId("console-status");
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveTextContent(/^$/);
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    await screen.findByTestId("console-result");
    expect(status).toHaveTextContent("Passed. Redacted 2 matches.");
    expect(screen.getByTestId("console-result").closest("[aria-live]")).toBeNull();
  });

  it("words the verdict from counts only", () => {
    expect(consoleVerdict({ text: null, blocked: true, results: [] })).toBe("Blocked: the text would not be passed on.");
    expect(consoleVerdict({ text: "x", blocked: false, results: [] })).toBe("Passed.");
    expect(consoleVerdict({ text: "x", blocked: false, results: [
      { index: 0, processor_type: "pii_redact", action: "redact", match_counts: { email: 1 } },
      { index: 1, processor_type: "max_length", action: "truncate", match_counts: {} },
    ] })).toBe("Passed. Redacted 1 match. Truncated.");
  });
});
