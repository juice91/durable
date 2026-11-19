// W2.2: feedback on a trace and a span, and "Add to test suite" from the trace detail.
import React from "react";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TraceFeedback } from "./TraceFeedback";
import { AddToDataset } from "./AddToDataset";
import { SpanDetail } from "./SpanDetail";
import { addTraceFeedback, listTraceFeedback, type TraceRun, type TraceSpan } from "@/lib/traces";
import { addCase } from "@/lib/evals";
import { listAllSuites } from "@/lib/evalOverview";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";

vi.mock("@/lib/traces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/traces")>()),
  listTraceFeedback: vi.fn(),
  addTraceFeedback: vi.fn(),
}));
vi.mock("@/lib/evals", async (importOriginal) => ({ ...(await importOriginal<object>()), addCase: vi.fn() }));
vi.mock("@/lib/evalOverview", async (importOriginal) => ({ ...(await importOriginal<object>()), listAllSuites: vi.fn() }));

const list = vi.mocked(listTraceFeedback);
const add = vi.mocked(addTraceFeedback);
const FB = { id: "f1", trace_id: "run-1", span_id: null, rating: "up" as const, score: 3, comment: "nice", created_by: "alice", created_at: "2026-11-16T10:00:00Z" };

beforeEach(() => {
  vi.clearAllMocks();
  list.mockResolvedValue([]);
});
afterEach(cleanup);

describe("TraceFeedback", () => {
  it("lists existing feedback with who and when, and the empty state", async () => {
    list.mockResolvedValue([FB, { ...FB, id: "f2", span_id: "run:run-1", rating: "down", comment: "span-only" }]);
    render(<TraceFeedback runId="run-1" />);
    const items = await screen.findAllByTestId("feedback-item");
    // The trace's own feedback excludes its spans'.
    expect(items).toHaveLength(1);
    expect(items[0]).toHaveTextContent("Thumbs up");
    expect(items[0]).toHaveTextContent("score 3");
    expect(items[0]).toHaveTextContent("alice");
    expect(items[0]).toHaveTextContent("nice");
    cleanup();
    list.mockResolvedValue([]);
    render(<TraceFeedback runId="run-1" />);
    expect(await screen.findByText("No feedback yet.")).toBeInTheDocument();
  });

  it("needs a rating, validates the score and posts the trace feedback, then reloads the list", async () => {
    render(<TraceFeedback runId="run-1" />);
    await screen.findByText("No feedback yet.");
    fireEvent.click(screen.getByRole("button", { name: "Send feedback" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose thumbs up or thumbs down.");
    expect(add).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Thumbs down" }));
    expect(screen.getByRole("button", { name: "Thumbs down" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.change(screen.getByLabelText("Comment (optional)"), { target: { value: " too slow " } });
    fireEvent.change(screen.getByLabelText("Score (optional)"), { target: { value: "2.5" } });
    add.mockResolvedValue({ ...FB, rating: "down", comment: "too slow", score: 2.5 });
    list.mockResolvedValue([{ ...FB, rating: "down", comment: "too slow", score: 2.5 }]);
    fireEvent.click(screen.getByRole("button", { name: "Send feedback" }));
    await waitFor(() => expect(add).toHaveBeenCalledWith("run-1", { rating: "down", comment: "too slow", score: 2.5, span_id: null }));
    expect(await screen.findByTestId("feedback-item")).toHaveTextContent("too slow");
    expect(screen.getByLabelText("Comment (optional)")).toHaveValue("");
  });

  it("shows the API error and keeps the form", async () => {
    add.mockRejectedValue(new Error("Trace not found"));
    render(<TraceFeedback runId="run-1" />);
    await screen.findByText("No feedback yet.");
    fireEvent.click(screen.getByRole("button", { name: "Thumbs up" }));
    fireEvent.click(screen.getByRole("button", { name: "Send feedback" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Trace not found");
    expect(screen.getByRole("button", { name: "Thumbs up" })).toHaveAttribute("aria-pressed", "true");
  });

  it("a span's form posts with its span id and lists only that span's feedback", async () => {
    const span = { id: "run:run-1", parent_id: null, kind: "run", name: "run", status: "ok", start: null, end: null, attributes: {} } as unknown as TraceSpan;
    list.mockResolvedValue([{ ...FB, span_id: "run:run-1", comment: "on the span" }]);
    render(<SpanDetail span={span} runId="run-1" />);
    expect(list).toHaveBeenCalledWith("run-1", expect.objectContaining({ spanId: "run:run-1" }));
    expect(await screen.findByTestId("feedback-item")).toHaveTextContent("on the span");
    expect(screen.getByTestId("span-feedback")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Thumbs up" }));
    add.mockResolvedValue(FB);
    fireEvent.click(screen.getByRole("button", { name: "Send feedback" }));
    await waitFor(() => expect(add).toHaveBeenCalledWith("run-1", expect.objectContaining({ rating: "up", span_id: "run:run-1" })));
  });

  it("without a run id the span detail shows no feedback (older callers)", () => {
    const span = { id: "s", parent_id: null, kind: "run", name: "run", status: "ok", start: null, end: null, attributes: {} } as unknown as TraceSpan;
    render(<SpanDetail span={span} />);
    expect(screen.queryByTestId("span-feedback")).toBeNull();
  });

  it("has no axe violations", async () => {
    list.mockResolvedValue([FB]);
    const { container } = render(<TraceFeedback runId="run-1" />);
    await screen.findByTestId("feedback-item");
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});

const RUN = { run_id: "11111111-2222-3333-4444-555555555555", kind: "agent", input: "What is 2+2?", output: "4" } as unknown as TraceRun;
const SUITES = [
  { id: "s1", name: "Math", agent_name: "Tutor" },
  { id: "s2", name: "Other", agent_name: null },
] as never;

describe("AddToDataset", () => {
  beforeEach(() => {
    vi.mocked(listAllSuites).mockResolvedValue({ items: SUITES, next_cursor: null } as never);
  });

  it("creates a case from the trace's input with the output as the editable expected output", async () => {
    render(<AddToDataset run={RUN} />);
    fireEvent.click(screen.getByRole("button", { name: "Add to test suite" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("option", { name: "Tutor / Math" });
    expect(within(dialog).getByLabelText("Input (from the trace)")).toHaveValue("What is 2+2?");
    expect(within(dialog).getByLabelText("Input (from the trace)")).toHaveAttribute("readonly");
    expect(within(dialog).getByLabelText("Expected output")).toHaveValue("4");
    expect(within(dialog).getByLabelText("Case name")).toHaveValue("trace 11111111");

    // No dataset chosen yet.
    fireEvent.click(within(dialog).getByRole("button", { name: "Add case" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Choose a test suite.");
    expect(addCase).not.toHaveBeenCalled();

    fireEvent.change(within(dialog).getByLabelText("Test suite"), { target: { value: "s1" } });
    fireEvent.change(within(dialog).getByLabelText("Expected output"), { target: { value: "four" } });
    vi.mocked(addCase).mockResolvedValue({} as never);
    fireEvent.click(within(dialog).getByRole("button", { name: "Add case" }));
    await waitFor(() => expect(addCase).toHaveBeenCalledWith("s1", {
      name: "trace 11111111", input: "What is 2+2?", expected_output: "four",
      metadata: { source_trace_id: RUN.run_id }, evaluators: [],
    }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("shows the API error (a name already in the suite) and keeps the dialog open", async () => {
    vi.mocked(addCase).mockRejectedValue(new Error("A case with this name already exists"));
    render(<AddToDataset run={RUN} />);
    fireEvent.click(screen.getByRole("button", { name: "Add to test suite" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("option", { name: "Tutor / Math" });
    fireEvent.change(within(dialog).getByLabelText("Test suite"), { target: { value: "s1" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add case" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("already exists");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("has no axe violations with the dialog open", async () => {
    render(<AddToDataset run={RUN} />);
    fireEvent.click(screen.getByRole("button", { name: "Add to test suite" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("option", { name: "Tutor / Math" });
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
