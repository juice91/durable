import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ToolsPage from "./page";
import { listTools, type ToolDefinition } from "@/lib/tools";
import { startToolRun, ToolRunError, mapToolRunError } from "@/lib/toolRuns";
import { getRun, subscribeRun, type SubscribeHandlers } from "@/lib/runs";
import { toolRunLedger } from "@/lib/toolRunLedger";
import { routes } from "@/lib/routes";

vi.mock("@/lib/tools", () => ({ listTools: vi.fn() }));
vi.mock("@/lib/toolRuns", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/toolRuns")>();
  return { ...actual, startToolRun: vi.fn() };
});
vi.mock("@/lib/runs", () => ({ getRun: vi.fn(), subscribeRun: vi.fn() }));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a>,
}));

const tools: ToolDefinition[] = [
  {
    id: "tickets.create",
    title: "Create ticket",
    description: "Creates a support ticket",
    side_effect: true,
    external_idempotent: false,
    input_schema: {
      type: "object" as const,
      required: ["subject"],
      properties: { subject: { type: "string" as const, title: "Subject" } },
    },
    allowed: true,
  },
  {
    id: "clock.now",
    title: "Current time",
    description: "Returns the current time",
    side_effect: false,
    external_idempotent: true,
    input_schema: { type: "object" as const, properties: {} },
    allowed: false,
  },
];

describe("ToolsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    toolRunLedger.reset();
    vi.mocked(listTools).mockResolvedValue(tools);
    vi.mocked(subscribeRun).mockReturnValue(() => undefined);
  });

  it("searches the catalog and displays policy badges", async () => {
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });

    expect(screen.getByText("Side effect")).toBeInTheDocument();
    expect(screen.getByText("Disallowed")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search tools"), { target: { value: "clock" } });
    const catalog = screen.getByLabelText("Tool catalog");
    expect(within(catalog).queryByText("Create ticket")).not.toBeInTheDocument();
    expect(within(catalog).getByText("Current time")).toBeInTheDocument();
  });

  it("keeps form and raw JSON modes in sync", async () => {
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });

    fireEvent.change(screen.getByLabelText(/Subject \*/), { target: { value: "Printer down" } });
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    expect((screen.getByLabelText("Tool input JSON") as HTMLTextAreaElement).value).toContain("Printer down");

    fireEvent.change(screen.getByLabelText("Tool input JSON"), {
      target: { value: JSON.stringify({ subject: "Network down" }) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Form" }));
    expect(screen.getByLabelText(/Subject \*/)).toHaveValue("Network down");
  });

  it("shows invalid JSON inline and leaves running disabled", async () => {
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    fireEvent.change(screen.getByLabelText("Tool input JSON"), { target: { value: "{" } });

    expect(screen.getByText("Enter valid JSON")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
  });

  it("marks the selected input mode as pressed", async () => {
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    expect(screen.getByRole("button", { name: "Form" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "JSON" })).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    expect(screen.getByRole("button", { name: "JSON" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Form" })).toHaveAttribute("aria-pressed", "false");
  });

  it("keeps a late 202 for the previous tool as a persistent note with a run link, without aborting", async () => {
    let resolve: (value: Awaited<ReturnType<typeof startToolRun>>) => void = () => undefined;
    vi.mocked(startToolRun).mockImplementationOnce((...args) => {
      expect(args).toHaveLength(3); // no AbortSignal: the request is never aborted
      return new Promise((res) => { resolve = res; });
    });
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.change(screen.getByLabelText(/Subject \*/), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Current time"));
    expect(await screen.findByRole("heading", { name: "Current time" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run" })).not.toBeDisabled();

    await act(async () => resolve({ run_id: "stale-run", kind: "tool", tool_id: "tickets.create", status: "queued", display_status: "queued", trace_id: null }));
    // Not in the current tool's panel...
    expect(screen.queryByTestId("tool-run-result")).not.toBeInTheDocument();
    expect(subscribeRun).not.toHaveBeenCalled();
    // ...but announced, with a link, and it stays after switching tools again.
    const notes = screen.getByTestId("tool-runs-started-elsewhere");
    expect(notes).toHaveTextContent("Run started for Create ticket");
    expect(within(notes).getByRole("link", { name: "Open run" })).toHaveAttribute("href", routes.run("stale-run"));
    fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Create ticket"));
    expect(screen.getByTestId("tool-runs-started-elsewhere")).toHaveTextContent("Run started for Create ticket");
  });

  it("ignores a late 503 banner for the previous tool", async () => {
    let reject: (reason: unknown) => void = () => undefined;
    vi.mocked(startToolRun).mockImplementationOnce(() => new Promise((_res, rej) => { reject = rej; }));
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Current time"));
    await act(async () => reject(new ToolRunError(503, "Run recorded but could not be started", {})));
    expect(screen.queryByTestId("tool-run-banner")).not.toBeInTheDocument();
    expect(screen.queryByText(/same run id/)).not.toBeInTheDocument();
  });

  it("clicking the already-selected tool during a run changes nothing", async () => {
    let resolve: (value: Awaited<ReturnType<typeof startToolRun>>) => void = () => undefined;
    vi.mocked(startToolRun).mockImplementationOnce(() => new Promise((res) => { resolve = res; }));
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.change(screen.getByLabelText(/Subject \*/), { target: { value: "keep me" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Create ticket"));

    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    expect(screen.getByLabelText(/Subject \*/)).toHaveValue("keep me");
    await act(async () => resolve({ run_id: "run-same", kind: "tool", tool_id: "tickets.create", status: "queued", display_status: "queued", trace_id: null }));
    expect(screen.getByTestId("tool-run-link")).toHaveAttribute("href", routes.run("run-same"));
    expect(screen.queryByTestId("tool-runs-started-elsewhere")).not.toBeInTheDocument();
  });

  describe("run id reuse when the outcome is unknown", () => {
    const accepted = (runId: string, toolId = "tickets.create") => ({ run_id: runId, kind: "tool" as const, tool_id: toolId, status: "queued", display_status: "queued" as const, trace_id: null });
    const runIdOfCall = (n: number) => vi.mocked(startToolRun).mock.calls[n][2];

    async function fillSubject(value: string) {
      fireEvent.change(await screen.findByLabelText(/Subject \*/), { target: { value } });
    }

    it("after a failed fetch, says the request may have arrived and the next Run reuses the run id", async () => {
      vi.mocked(startToolRun)
        .mockRejectedValueOnce(new TypeError("Failed to fetch"))
        .mockResolvedValueOnce(accepted("r-1"));
      render(<ToolsPage />);
      await screen.findByRole("heading", { name: "Create ticket" });
      await fillSubject("Printer down");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));

      expect(await screen.findByTestId("tool-run-banner")).toHaveTextContent("The request may have reached the server; running again will reuse the same run id");
      expect(screen.getByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "uncertain");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByTestId("tool-run-result");
      expect(runIdOfCall(1)).toBe(runIdOfCall(0));
      // The 202 settles it: a later Run of the same input is a new run.
      expect(screen.queryByTestId("tool-run-reuse-hint")).not.toBeInTheDocument();
    });

    it("treats an unreadable 202 body like a lost response", async () => {
      vi.mocked(startToolRun).mockRejectedValueOnce(new SyntaxError("Unexpected end of JSON input")).mockResolvedValueOnce(accepted("r-2"));
      render(<ToolsPage />);
      await screen.findByRole("heading", { name: "Create ticket" });
      await fillSubject("x");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByTestId("tool-run-reuse-hint");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByTestId("tool-run-result");
      expect(runIdOfCall(1)).toBe(runIdOfCall(0));
    });

    it("a 422 for the input forgets the id, so a corrected input is a new request", async () => {
      vi.mocked(startToolRun).mockRejectedValueOnce(mapToolRunError(422, { detail: "tool 'tickets.create' is not allowed" }));
      render(<ToolsPage />);
      await screen.findByRole("heading", { name: "Create ticket" });
      await fillSubject("x");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByTestId("tool-run-banner");
      expect(screen.queryByTestId("tool-run-reuse-hint")).not.toBeInTheDocument();
    });

    it("reuses the id after the page unmounts mid-request and is opened again", async () => {
      vi.mocked(startToolRun).mockImplementationOnce(() => new Promise(() => undefined)).mockResolvedValueOnce(accepted("r-3"));
      const first = render(<ToolsPage />);
      await screen.findByRole("heading", { name: "Create ticket" });
      await fillSubject("Printer down");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      first.unmount();

      render(<ToolsPage />);
      await screen.findByRole("heading", { name: "Create ticket" });
      await fillSubject("Printer down");
      expect(await screen.findByTestId("tool-run-reuse-hint")).toHaveTextContent("running again will reuse the same run id");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByTestId("tool-run-result");
      expect(runIdOfCall(1)).toBe(runIdOfCall(0));
    });

    it("reuses the id after a remount even when sessionStorage refuses writes (quota)", async () => {
      const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
        throw new DOMException("The quota has been exceeded.", "QuotaExceededError");
      });
      vi.mocked(startToolRun).mockRejectedValueOnce(new TypeError("Failed to fetch")).mockResolvedValueOnce(accepted("r-q"));
      const first = render(<ToolsPage />);
      await screen.findByRole("heading", { name: "Create ticket" });
      await fillSubject("Printer down");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByTestId("tool-run-banner");
      first.unmount();

      render(<ToolsPage />);
      await screen.findByRole("heading", { name: "Create ticket" });
      await fillSubject("Printer down");
      expect(await screen.findByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "uncertain");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByTestId("tool-run-result");
      expect(runIdOfCall(1)).toBe(runIdOfCall(0));
      setItem.mockRestore();
    });

    it("a 202 that arrives after unmount shows an Open run note when the page is opened again", async () => {
      let resolve: (value: ReturnType<typeof accepted>) => void = () => undefined;
      vi.mocked(startToolRun).mockImplementationOnce(() => new Promise((res) => { resolve = res; }));
      const first = render(<ToolsPage />);
      await screen.findByRole("heading", { name: "Create ticket" });
      await fillSubject("x");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      first.unmount();
      await act(async () => resolve(accepted("r-late")));

      render(<ToolsPage />);
      const notes = await screen.findByTestId("tool-runs-started-elsewhere");
      expect(notes).toHaveTextContent("Run started for Create ticket");
      expect(within(notes).getByRole("link", { name: "Open run" })).toHaveAttribute("href", routes.run("r-late"));
      await fillSubject("x");
      expect(screen.queryByTestId("tool-run-reuse-hint")).not.toBeInTheDocument();
    });

    it("keeps one pending id per tool: overlapping 503s for two tools do not overwrite each other", async () => {
      let rejectA: (reason: unknown) => void = () => undefined;
      vi.mocked(startToolRun)
        .mockImplementationOnce(() => new Promise((_res, rej) => { rejectA = rej; })) // A: tickets.create
        .mockRejectedValueOnce(new ToolRunError(503, "Run recorded but could not be started", {})) // B: clock.now
        .mockResolvedValueOnce(accepted("b-ok", "clock.now"))
        .mockResolvedValueOnce(accepted("a-ok"));
      render(<ToolsPage />);
      await screen.findByRole("heading", { name: "Create ticket" });
      await fillSubject("x");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));

      fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Current time"));
      await screen.findByRole("heading", { name: "Current time" });
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByTestId("tool-run-banner");
      await act(async () => rejectA(new ToolRunError(503, "Run recorded but could not be started", {})));

      // B keeps its own id.
      expect(screen.getByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "not_started");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByTestId("tool-run-result");
      expect(runIdOfCall(2)).toBe(runIdOfCall(1));

      // A keeps its own id too.
      fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Create ticket"));
      await fillSubject("x");
      expect(screen.getByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "not_started");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByTestId("tool-run-result");
      expect(runIdOfCall(3)).toBe(runIdOfCall(0));
      expect(runIdOfCall(0)).not.toBe(runIdOfCall(1));
    });
  });

  it("disables Run while the request is in flight and sends the input with a run id", async () => {
    let resolve: (value: Awaited<ReturnType<typeof startToolRun>>) => void = () => undefined;
    vi.mocked(startToolRun).mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.change(screen.getByLabelText(/Subject \*/), { target: { value: "Printer down" } });

    const run = screen.getByRole("button", { name: "Run" });
    fireEvent.click(run);
    expect(run).toBeDisabled();
    expect(startToolRun).toHaveBeenCalledWith("tickets.create", { subject: "Printer down" }, expect.any(String));

    await act(async () => resolve({ run_id: "11111111-2222-3333-4444-555555555555", kind: "tool", tool_id: "tickets.create", status: "queued", display_status: "queued", trace_id: null }));
    expect(run).not.toBeDisabled();
    expect(screen.getByTestId("tool-run-link")).toHaveAttribute("href", routes.run("11111111-2222-3333-4444-555555555555"));
  });

  it("maps 422 validation errors onto the form fields", async () => {
    vi.mocked(startToolRun).mockRejectedValue(mapToolRunError(422, {
      detail: [{ loc: ["body", "input", "subject"], msg: "Field required", type: "missing" }],
    }));
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByTestId("field-error-subject")).toHaveTextContent("Field required");
    expect(screen.getByLabelText(/Subject \*/)).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByTestId("tool-run-banner")).toHaveTextContent("Fix the highlighted input fields");
  });

  it("shows a string 422 (tool not allowed) as a banner", async () => {
    vi.mocked(startToolRun).mockRejectedValue(new ToolRunError(422, "tool 'clock.now' is not allowed on this platform", {}));
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Current time"));
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByTestId("tool-run-banner")).toHaveTextContent("is not allowed on this platform");
    expect(screen.queryByTestId("tool-run-result")).not.toBeInTheDocument();
  });

  it("follows the run and shows needs_attention without retrying", async () => {
    vi.mocked(startToolRun).mockResolvedValue({ run_id: "run-1", kind: "tool", tool_id: "tickets.create", status: "queued", display_status: "queued", trace_id: null });
    let handlers: SubscribeHandlers = {};
    vi.mocked(subscribeRun).mockImplementation((_id, h) => { handlers = h; return () => undefined; });
    vi.mocked(getRun).mockResolvedValue({
      run_id: "run-1", kind: "tool", agent_id: null, version: null, tool_id: "tickets.create", status: "needs_attention",
      display_status: "needs_attention", input: "{}", output: null, error: "side effect outcome unknown", heartbeat_at: null,
      created_at: "", updated_at: "", trace_id: null, conversation_id: null, turn: null,
    });
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.change(screen.getByLabelText(/Subject \*/), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByTestId("tool-run-result");

    act(() => {
      handlers.onEvent?.({ seq: 2, type: "run_failed", data: { status: "needs_attention", error: "side effect outcome unknown", error_code: "side_effect_outcome_unknown" }, ts: "" });
      handlers.onEnd?.("run_failed");
    });
    expect(await screen.findByTestId("tool-run-needs-attention")).toHaveTextContent("Needs attention: the tool may have run; check before retrying");
    expect(screen.getByTestId("tool-run-error-code")).toHaveTextContent("side_effect_outcome_unknown");
    await waitFor(() => expect(getRun).toHaveBeenCalledWith("run-1"));
    expect(startToolRun).toHaveBeenCalledTimes(1);
  });

  it("shows the pretty-printed result of a completed run", async () => {
    vi.mocked(startToolRun).mockResolvedValue({ run_id: "run-2", kind: "tool", tool_id: "tickets.create", status: "queued", display_status: "queued", trace_id: null });
    let handlers: SubscribeHandlers = {};
    vi.mocked(subscribeRun).mockImplementation((_id, h) => { handlers = h; return () => undefined; });
    vi.mocked(getRun).mockResolvedValue({
      run_id: "run-2", kind: "tool", agent_id: null, version: null, tool_id: "tickets.create", status: "completed",
      display_status: "completed", input: "{}", output: '{"ticket":"T-1"}', error: null, heartbeat_at: null,
      created_at: "", updated_at: "", trace_id: null, conversation_id: null, turn: null,
    });
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.change(screen.getByLabelText(/Subject \*/), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByTestId("tool-run-result");
    act(() => handlers.onEnd?.("run_completed"));

    expect(await screen.findByTestId("tool-run-output")).toHaveTextContent('"ticket": "T-1"');
  });
});
