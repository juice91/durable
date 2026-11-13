import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ProcessorsPage from "./page";
import { api, ApiError, type Agent } from "@/lib/api";
import { loadAgentProcessorSpecs, processors, type ProcessorCatalog, type ProcessorResultItem } from "@/lib/processors";
import { routes } from "@/lib/routes";

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

describe("ProcessorsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(processors.catalog).mockResolvedValue(catalog);
    vi.mocked(processors.results).mockResolvedValue({ items: [blocked], next_before: null });
    vi.mocked(api.agents.list).mockResolvedValue(agents);
    vi.mocked(loadAgentProcessorSpecs).mockResolvedValue([
      { agent: agents[0], draft: null, latest: { input_processors: [{ type: "pii_redact", config: {} }] } },
    ]);
  });

  it("renders catalog cards with config schema and the agents using each type", async () => {
    render(<ProcessorsPage />);
    const cards = await screen.findAllByTestId("processor-card");
    expect(cards).toHaveLength(3);
    const pii = cards.find((card) => card.dataset.processorType === "pii_redact")!;
    expect(within(pii).getByText("PII redaction")).toBeInTheDocument();
    expect(within(pii).getByTestId("processor-config-fields")).toHaveTextContent("kinds");
    const usage = await within(pii).findByTestId("processor-usage");
    expect(within(usage).getByRole("link", { name: "Support bot" })).toHaveAttribute("href", routes.agentEditor("agent-1"));
    expect(usage).toHaveTextContent("v2");
    const moderation = cards.find((card) => card.dataset.processorType === "moderation")!;
    expect(within(moderation).getByText(/test in the playground/)).toBeInTheDocument();
    expect(within(moderation).getByTestId("processor-usage-empty")).toBeInTheDocument();
  });

  it("lists recent outcomes with counts only and a link to the run", async () => {
    render(<ProcessorsPage />);
    const row = await screen.findByTestId("result-row");
    expect(within(row).getByRole("link", { name: `Open trace of run ${RUN_ID}` })).toHaveAttribute("href", routes.trace(RUN_ID));
    expect(row).toHaveTextContent("email ×2");
    expect(row).toHaveTextContent("block");
    expect(row).toHaveTextContent("input");
    expect(screen.getByText(/Matched text is never stored/)).toBeInTheDocument();
  });

  it("applies filters and validates the run id", async () => {
    render(<ProcessorsPage />);
    await screen.findByTestId("result-row");
    await screen.findByRole("option", { name: "Support bot" });
    const filters = within(screen.getByRole("form", { name: "Filter outcomes" }));

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
      agent_id: "agent-1", action: "block", phase: "input", run_id: RUN_ID, limit: 50,
    })));
    expect(await screen.findByTestId("results-empty")).toHaveTextContent("No outcomes match these filters");
  });

  it("pages older outcomes with next_before", async () => {
    vi.mocked(processors.results)
      .mockResolvedValueOnce({ items: [blocked], next_before: "cursor-1" })
      .mockResolvedValueOnce({ items: [{ ...blocked, seq: 2, action: "redact" }], next_before: null });
    render(<ProcessorsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Load older outcomes" }));
    await waitFor(() => expect(screen.getAllByTestId("result-row")).toHaveLength(2));
    expect(processors.results).toHaveBeenLastCalledWith(expect.objectContaining({ before: "cursor-1" }));
    expect(screen.queryByRole("button", { name: "Load older outcomes" })).not.toBeInTheDocument();
  });

  it("shows the empty state when there are no outcomes", async () => {
    vi.mocked(processors.results).mockResolvedValue({ items: [], next_before: null });
    render(<ProcessorsPage />);
    expect(await screen.findByTestId("results-empty")).toHaveTextContent("No processor outcomes yet");
  });

  it("runs the test console and shows outcomes and processed text", async () => {
    vi.mocked(processors.test).mockResolvedValue({
      text: "mail [REDACTED:email]", blocked: false,
      results: [{ index: 0, processor_type: "pii_redact", action: "redact", match_counts: { email: 1 } }],
    });
    render(<ProcessorsPage />);
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
    render(<ProcessorsPage />);
    const cards = await screen.findAllByTestId("processor-card");
    const maxLength = cards.find((card) => card.dataset.processorType === "max_length")!;
    fireEvent.click(within(maxLength).getByRole("button", { name: "Try in test console" }));
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
    render(<ProcessorsPage />);
    const option = await screen.findByRole("option", { name: "Moderation (test in the playground)" });
    expect(option).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    expect(await screen.findByTestId("console-error")).toHaveTextContent("Test it in the playground");
  });

  it("shows an error when the catalog cannot be loaded", async () => {
    vi.mocked(processors.catalog).mockRejectedValue(new ApiError(500, "HTTP 500"));
    render(<ProcessorsPage />);
    expect(await screen.findByTestId("catalog-error")).toHaveTextContent("HTTP 500");
    expect(screen.getByText("The test console needs the processor catalog.")).toBeInTheDocument();
  });

  it("marks agents whose latest version could not be read as unknown", async () => {
    vi.mocked(loadAgentProcessorSpecs).mockResolvedValue([{ agent: agents[0], draft: null, latest: null, failed: true }]);
    render(<ProcessorsPage />);
    const cards = await screen.findAllByTestId("processor-card");
    const pii = cards.find((card) => card.dataset.processorType === "pii_redact")!;
    expect(await within(pii).findByTestId("processor-usage-unknown")).toHaveTextContent("unknown: v2 could not be read");
    expect(within(pii).queryByTestId("processor-usage-empty")).not.toBeInTheDocument();
  });

  it("shows a 422 message with its field location", async () => {
    vi.mocked(processors.test).mockRejectedValue(new ApiError(422, "processors.0.config.max_chars: Input should be greater than or equal to 1"));
    render(<ProcessorsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Run test" }));
    expect(await screen.findByTestId("console-error")).toHaveTextContent("processors.0.config.max_chars: Input should be greater than or equal to 1");
  });

  it("clears the previous result when the sample or the steps change", async () => {
    vi.mocked(processors.test).mockResolvedValue({ text: "ok", blocked: false, results: [{ index: 0, processor_type: "pii_redact", action: "allow", match_counts: {} }] });
    render(<ProcessorsPage />);
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
    render(<ProcessorsPage />);
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
    render(<ProcessorsPage />);
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
});
