import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ToolsPage from "./page";
import { listTools, type ToolDefinition } from "@/lib/tools";
import { mapToolRunError, startToolRun } from "@/lib/toolRuns";
import { getRun, subscribeRun, type Run, type RunEvent, type RunStatus, type SubscribeHandlers } from "@/lib/runs";
import { canonicalInputKey, MAX_NOTES, toolRunLedger } from "@/lib/toolRunLedger";

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
    allowed: true,
    input_schema: {
      type: "object" as const,
      properties: {
        subject: { type: "string" as const, title: "Subject" },
        options: {
          type: "object" as const,
          title: "Options",
          properties: { retries: { type: "integer" as const, title: "Retries" } },
        },
        tags: { type: "array" as const, title: "Tags", items: { type: "string" as const } },
      },
    },
  },
  {
    id: "clock.now",
    title: "Current time",
    description: "Returns the current time",
    side_effect: false,
    external_idempotent: true,
    allowed: false,
    input_schema: { type: "object" as const, properties: {} },
  },
];

function accepted(runId: string) {
  return {
    run_id: runId,
    kind: "tool" as const,
    tool_id: "tickets.create",
    status: "queued",
    display_status: "queued" as const,
    trace_id: null,
  };
}

function runRow(runId: string, status: RunStatus, extra: Partial<Run> = {}): Run {
  return {
    run_id: runId,
    kind: "tool",
    agent_id: null,
    version: null,
    tool_id: "tickets.create",
    status,
    display_status: status,
    input: "{}",
    output: null,
    error: null,
    heartbeat_at: null,
    created_at: "",
    updated_at: "",
    trace_id: null,
    conversation_id: null,
    turn: null,
    ...extra,
  };
}

async function renderTools() {
  render(<ToolsPage />);
  await screen.findByRole("heading", { name: "Create ticket" });
}

describe("Tools run acceptance", () => {
  beforeEach(() => {
    toolRunLedger.reset();
    vi.mocked(startToolRun).mockReset();
    vi.mocked(subscribeRun).mockReset();
    vi.mocked(getRun).mockReset();
    vi.mocked(listTools).mockReset();
    vi.mocked(listTools).mockResolvedValue(tools);
    vi.mocked(subscribeRun).mockReturnValue(() => undefined);
  });

  it("disables Run while the request is in flight and calls startToolRun with three arguments", async () => {
    let resolve: (value: Awaited<ReturnType<typeof startToolRun>>) => void = () => undefined;
    vi.mocked(startToolRun).mockImplementation((...args) => {
      expect(args).toHaveLength(3);
      return new Promise((res) => { resolve = res; });
    });
    await renderTools();
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
    const run = screen.getByRole("button", { name: "Run" });
    fireEvent.click(run);

    expect(run).toBeDisabled();
    expect(run).toHaveAttribute("aria-busy", "true");
    expect(startToolRun).toHaveBeenCalledWith("tickets.create", { subject: "Printer down" }, expect.any(String));
    await act(async () => resolve(accepted("run-flight")));
    expect(run).toBeEnabled();
    expect(run).toHaveAttribute("aria-busy", "false");
  });

  it("follows a 202 to the output and links to the run", async () => {
    vi.mocked(startToolRun).mockResolvedValue(accepted("run-ok"));
    let handlers: SubscribeHandlers = {};
    vi.mocked(subscribeRun).mockImplementation((_id, next) => { handlers = next; return () => undefined; });
    vi.mocked(getRun).mockResolvedValue(runRow("run-ok", "completed", { output: '{"ticket":"T-9"}' }));
    await renderTools();
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByTestId("tool-run-link")).toHaveAttribute("href", "/runs/run-ok");

    act(() => handlers.onEnd?.("run_completed"));
    expect(await screen.findByTestId("tool-run-output")).toHaveTextContent('"ticket": "T-9"');
    expect(screen.getByTestId("tool-run-result")).toHaveAttribute("data-status", "completed");
    expect(screen.queryByTestId("tool-runs-started-elsewhere")).not.toBeInTheDocument();
    await waitFor(() => expect(getRun).toHaveBeenCalledWith("run-ok"));
    expect(subscribeRun).toHaveBeenCalledWith("run-ok", expect.any(Object));
  });

  it("maps a 422 list onto nested fields, array items, and unmatched paths", async () => {
    vi.mocked(startToolRun).mockRejectedValue(mapToolRunError(422, {
      detail: [
        { loc: ["body", "input", "options", "retries"], msg: "Too small", type: "greater_than" },
        { loc: ["body", "input", "tags", "0"], msg: "Not a string", type: "string_type" },
        { loc: ["body", "input", "ghost"], msg: "Extra field", type: "extra" },
        { loc: ["body", "run_id"], msg: "bad id", type: "value_error" },
      ],
    }));
    await renderTools();
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByTestId("field-error-options.retries")).toHaveTextContent("Too small");
    expect(screen.getByTestId("field-error-tags")).toHaveTextContent("Not a string");
    expect(screen.getByTestId("field-errors-unmatched")).toHaveTextContent("ghost: Extra field");
    expect(screen.getByLabelText("Retries")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Tags")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByTestId("tool-run-banner")).toHaveTextContent("body.run_id: bad id");
    expect(screen.queryByTestId("tool-run-result")).not.toBeInTheDocument();
  });

  it("shows string 422, 404 and 409 details in the banner and does not reuse the run id", async () => {
    vi.mocked(startToolRun)
      .mockRejectedValueOnce(mapToolRunError(422, { detail: "tool 'clock.now' is not allowed on this platform" }))
      .mockRejectedValueOnce(mapToolRunError(404, { detail: "No such tool" }))
      .mockRejectedValueOnce(mapToolRunError(409, { detail: "run id already used" }));
    await renderTools();
    fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Current time"));
    expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
    expect(screen.getByText(/not in the platform allowlist/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByTestId("tool-run-banner")).toHaveTextContent("is not allowed on this platform");
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByTestId("tool-run-banner")).toHaveTextContent("No such tool");
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByTestId("tool-run-banner")).toHaveTextContent("run id already used");
    expect(screen.queryByTestId("tool-run-reuse-hint")).not.toBeInTheDocument();

    const ids = vi.mocked(startToolRun).mock.calls.map((call) => call[2]);
    expect(new Set(ids).size).toBe(3);
    expect(startToolRun).toHaveBeenCalledTimes(3);
  });

  it("reuses the run id after a 503 only when the user runs the same tool and input again", async () => {
    vi.mocked(startToolRun).mockRejectedValue(mapToolRunError(503, { detail: "Run recorded but could not be started" }));
    await renderTools();
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByTestId("tool-run-banner")).toHaveTextContent("Run recorded but could not be started");
    expect(screen.getByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "not_started");
    const firstId = vi.mocked(startToolRun).mock.calls[0][2];

    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
    expect(startToolRun).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(startToolRun).toHaveBeenCalledTimes(2));
    expect(vi.mocked(startToolRun).mock.calls[1][2]).toBe(firstId);
    expect(vi.mocked(startToolRun).mock.calls[1][1]).toEqual({ subject: "Printer down" });

    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Other" } });
    expect(screen.queryByTestId("tool-run-reuse-hint")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(startToolRun).toHaveBeenCalledTimes(3));
    expect(vi.mocked(startToolRun).mock.calls[2][2]).not.toBe(firstId);
  });

  it("keeps a late 503 run id for the same tool and input, without a banner on the tool switched to", async () => {
    let reject: (reason: unknown) => void = () => undefined;
    vi.mocked(startToolRun).mockImplementationOnce((...args) => {
      expect(args).toHaveLength(3);
      return new Promise((_resolve, next) => { reject = next; });
    });
    await renderTools();
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    const firstId = vi.mocked(startToolRun).mock.calls[0][2];
    fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Current time"));

    await act(async () => reject(mapToolRunError(503, { detail: "Run recorded but could not be started" })));
    expect(screen.queryByTestId("tool-run-banner")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tool-run-reuse-hint")).not.toBeInTheDocument();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
    expect(startToolRun).toHaveBeenCalledTimes(1);

    fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Create ticket"));
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
    expect(screen.getByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "not_started");
    vi.mocked(startToolRun).mockResolvedValue(accepted(String(firstId)));
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(startToolRun).toHaveBeenCalledTimes(2));
    expect(vi.mocked(startToolRun).mock.calls[1][2]).toBe(firstId);
    expect(screen.queryByTestId("tool-runs-started-elsewhere")).not.toBeInTheDocument();
  });

  it("keeps a late 202 as a dismissible note and ignores a click on the already selected tool", async () => {
    let resolve: (value: Awaited<ReturnType<typeof startToolRun>>) => void = () => undefined;
    vi.mocked(startToolRun).mockImplementationOnce((...args) => {
      expect(args).toHaveLength(3);
      return new Promise((res) => { resolve = res; });
    });
    await renderTools();
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Keep" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Create ticket"));
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    expect(screen.getByLabelText("Subject")).toHaveValue("Keep");

    fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Current time"));
    await act(async () => resolve(accepted("stale-run")));
    const notes = screen.getByTestId("tool-runs-started-elsewhere");
    expect(notes).toHaveTextContent("Run started for Create ticket");
    expect(within(notes).getByRole("link", { name: "Open run" })).toHaveAttribute("href", "/runs/stale-run");
    expect(screen.queryByTestId("tool-run-result")).not.toBeInTheDocument();
    expect(subscribeRun).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Dismiss note for run stale-run" }));
    expect(screen.queryByTestId("tool-runs-started-elsewhere")).not.toBeInTheDocument();
  });

  it("shows needs_attention and a failed run's error code without retrying", async () => {
    const handlers = new Map<string, SubscribeHandlers>();
    vi.mocked(subscribeRun).mockImplementation((id, next) => { handlers.set(id, next); return () => undefined; });
    vi.mocked(getRun).mockImplementation(async (id) => (
      id === "run-fail"
        ? runRow(id, "failed", { error: "ticket service down" })
        : runRow(id, "needs_attention", { error: "side effect outcome unknown" })
    ));
    const finish = async (id: string, event: RunEvent) => {
      await waitFor(() => expect(handlers.has(id)).toBe(true));
      await act(async () => {
        handlers.get(id)?.onEvent?.(event);
        handlers.get(id)?.onEnd?.("run_failed");
        await Promise.resolve();
        await Promise.resolve();
      });
    };
    vi.mocked(startToolRun).mockResolvedValueOnce(accepted("run-bad"));
    await renderTools();
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await finish("run-bad", {
      seq: 2,
      type: "run_failed",
      data: { status: "needs_attention", error: "side effect outcome unknown", error_code: "side_effect_outcome_unknown" },
      ts: "",
    });
    expect(await screen.findByTestId("tool-run-needs-attention")).toHaveTextContent("Needs attention: the tool may have run; check before retrying");
    expect(screen.getByTestId("tool-run-needs-attention")).toHaveTextContent("was not retried automatically");
    expect(screen.getByTestId("tool-run-error")).toHaveTextContent("side effect outcome unknown");
    expect(screen.getByTestId("tool-run-error-code")).toHaveTextContent("side_effect_outcome_unknown");
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
    expect(startToolRun).toHaveBeenCalledTimes(1);

    vi.mocked(startToolRun).mockResolvedValueOnce(accepted("run-fail"));
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await finish("run-fail", {
      seq: 4,
      type: "run_failed",
      data: { status: "failed", error: "ticket service down", error_code: "tool_error" },
      ts: "",
    });
    expect(await screen.findByTestId("tool-run-error")).toHaveTextContent("ticket service down");
    expect(screen.getByTestId("tool-run-error-code")).toHaveTextContent("tool_error");
    expect(screen.getByTestId("tool-run-result")).toHaveAttribute("data-status", "failed");
    expect(screen.queryByTestId("tool-run-needs-attention")).not.toBeInTheDocument();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
    expect(startToolRun).toHaveBeenCalledTimes(2);
  });

  it("reuses the same run id after a fetch throws, and does not retry by itself", async () => {
    vi.mocked(startToolRun)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(accepted("run-retry"));
    await renderTools();
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    const firstId = vi.mocked(startToolRun).mock.calls[0][2];
    const inputKey = canonicalInputKey({ subject: "Printer down" });
    expect(toolRunLedger.pending("tickets.create", inputKey)?.runId).toBe(firstId);

    const hint = await screen.findByTestId("tool-run-reuse-hint");
    expect(hint).toHaveAttribute("data-state", "uncertain");
    expect(screen.getByTestId("tool-run-banner")).toHaveTextContent("Failed to fetch");
    expect(screen.getByTestId("tool-run-banner")).toHaveTextContent("running again will reuse the same run id");
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
    expect(startToolRun).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByTestId("tool-run-result");
    expect(vi.mocked(startToolRun).mock.calls[1][2]).toBe(firstId);
    expect(vi.mocked(startToolRun).mock.calls[1]).toHaveLength(3);
    expect(screen.queryByTestId("tool-run-reuse-hint")).not.toBeInTheDocument();
  });

  it("keeps separate run ids when two tools receive overlapping 503s", async () => {
    let rejectTickets: (reason: unknown) => void = () => undefined;
    vi.mocked(startToolRun)
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectTickets = reject; }))
      .mockRejectedValueOnce(mapToolRunError(503, { detail: "Run recorded but could not be started" }))
      .mockResolvedValueOnce(accepted("clock-ok"))
      .mockResolvedValueOnce(accepted("ticket-ok"));
    await renderTools();
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Current time"));
    await screen.findByRole("heading", { name: "Current time" });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "not_started");
    await act(async () => rejectTickets(mapToolRunError(503, { detail: "Run recorded but could not be started" })));

    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByTestId("tool-run-result");
    expect(vi.mocked(startToolRun).mock.calls[2][2]).toBe(vi.mocked(startToolRun).mock.calls[1][2]);

    fireEvent.click(within(screen.getByLabelText("Tool catalog")).getByText("Create ticket"));
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "x" } });
    expect(screen.getByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "not_started");
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByTestId("tool-run-link");
    expect(vi.mocked(startToolRun).mock.calls[3][2]).toBe(vi.mocked(startToolRun).mock.calls[0][2]);
    expect(vi.mocked(startToolRun).mock.calls[0][2]).not.toBe(vi.mocked(startToolRun).mock.calls[1][2]);
  });

  it("reuses the run id after unmounting in the middle of the request", async () => {
    vi.mocked(startToolRun)
      .mockImplementationOnce(() => new Promise(() => undefined))
      .mockResolvedValueOnce(accepted("run-back"));
    const first = render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    const firstId = vi.mocked(startToolRun).mock.calls[0][2];
    expect(toolRunLedger.pending("tickets.create", canonicalInputKey({ subject: "Printer down" }))?.state).toBe("in_flight");
    first.unmount();

    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
    expect(await screen.findByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "in_flight");
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByTestId("tool-run-result");
    expect(vi.mocked(startToolRun).mock.calls[1][2]).toBe(firstId);
  });

  it("still reuses the run id when sessionStorage throws", async () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
    vi.mocked(startToolRun)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(accepted("mem-run"));
    try {
      const first = render(<ToolsPage />);
      await screen.findByRole("heading", { name: "Create ticket" });
      fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      const firstId = vi.mocked(startToolRun).mock.calls[0][2];
      expect(await screen.findByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "uncertain");
      first.unmount();

      render(<ToolsPage />);
      await screen.findByRole("heading", { name: "Create ticket" });
      fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
      expect(await screen.findByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "uncertain");
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByTestId("tool-run-result");
      expect(vi.mocked(startToolRun).mock.calls[1][2]).toBe(firstId);
    } finally {
      getItem.mockRestore();
      setItem.mockRestore();
    }
    expect(window.sessionStorage.getItem("eap.studio.toolRuns.v1")).toBeNull();
  });

  it("reuses the same run id when setItem throws QuotaExceededError and getItem still returns stale JSON", async () => {
    const olderKey = canonicalInputKey({ subject: "older" });
    toolRunLedger.remember({
      toolId: "tickets.create",
      toolTitle: "Create ticket",
      inputKey: olderKey,
      runId: "run-old",
      state: "uncertain",
    });
    const stale = window.sessionStorage.getItem("eap.studio.toolRuns.v1");
    expect(stale).toContain("run-old");
    expect(stale).not.toContain("Printer down");

    const originalGetItem = Storage.prototype.getItem;
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("The quota has been exceeded.", "QuotaExceededError");
    });
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(function (this: Storage, key: string) {
      if (key === "eap.studio.toolRuns.v1") return stale;
      return originalGetItem.call(this, key);
    });
    vi.mocked(startToolRun)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(accepted("run-quota"));
    try {
      await renderTools();
      fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      const firstId = vi.mocked(startToolRun).mock.calls[0][2];
      expect(firstId).not.toBe("run-old");
      const inputKey = canonicalInputKey({ subject: "Printer down" });
      expect(inputKey).toMatch(/^h1:[0-9a-f]{28}$/);
      expect(toolRunLedger.pending("tickets.create", inputKey)?.runId).toBe(firstId);
      expect(toolRunLedger.pending("tickets.create", olderKey)?.runId).toBe("run-old");
      expect(await screen.findByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "uncertain");
      expect(window.sessionStorage.getItem("eap.studio.toolRuns.v1")).toBe(stale);
      expect(stale).not.toContain(firstId);
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
      expect(startToolRun).toHaveBeenCalledTimes(1);

      fireEvent.click(screen.getByRole("button", { name: "Run" }));
      await screen.findByTestId("tool-run-result");
      expect(vi.mocked(startToolRun).mock.calls[1][2]).toBe(firstId);
      expect(vi.mocked(startToolRun).mock.calls[1]).toHaveLength(3);
      expect(window.sessionStorage.getItem("eap.studio.toolRuns.v1")).toBe(stale);
    } finally {
      getItem.mockRestore();
      setItem.mockRestore();
    }
  });

  it("keeps only the newest 20 notes and leaves pending run ids in place", async () => {
    expect(MAX_NOTES).toBe(20);
    vi.mocked(startToolRun)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(accepted("run-after-notes"));
    await renderTools();
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    const pendingId = vi.mocked(startToolRun).mock.calls[0][2];
    const inputKey = canonicalInputKey({ subject: "Printer down" });
    expect(await screen.findByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "uncertain");

    const otherKey = canonicalInputKey({});
    act(() => {
      toolRunLedger.remember({
        toolId: "clock.now",
        toolTitle: "Current time",
        inputKey: otherKey,
        runId: "pending-other",
        state: "not_started",
      });
      for (let i = 0; i < 25; i += 1) {
        toolRunLedger.addNote({ runId: `note-${i}`, toolId: "clock.now", toolTitle: "Current time" });
      }
    });

    const notes = screen.getByTestId("tool-runs-started-elsewhere");
    expect(within(notes).getAllByRole("status")).toHaveLength(20);
    expect(within(notes).queryByRole("button", { name: "Dismiss note for run note-0" })).not.toBeInTheDocument();
    expect(within(notes).queryByRole("button", { name: "Dismiss note for run note-4" })).not.toBeInTheDocument();
    expect(within(notes).getByRole("button", { name: "Dismiss note for run note-5" })).toBeInTheDocument();
    expect(within(notes).getByRole("button", { name: "Dismiss note for run note-24" })).toBeInTheDocument();
    expect(toolRunLedger.notes().map((note) => note.runId)).toEqual(
      Array.from({ length: 20 }, (_, index) => `note-${index + 5}`),
    );
    expect(toolRunLedger.pending("tickets.create", inputKey)?.runId).toBe(pendingId);
    expect(toolRunLedger.pending("clock.now", otherKey)?.runId).toBe("pending-other");

    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByTestId("tool-run-result");
    expect(vi.mocked(startToolRun).mock.calls[1][2]).toBe(pendingId);
    expect(toolRunLedger.pending("clock.now", otherKey)?.runId).toBe("pending-other");
  });

  it("restores the pending run id after reloadFromStorage", async () => {
    vi.mocked(startToolRun)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(accepted("run-reloaded"));
    const first = render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    const firstId = vi.mocked(startToolRun).mock.calls[0][2];
    const inputKey = canonicalInputKey({ subject: "Printer down" });
    expect(inputKey).toMatch(/^h1:[0-9a-f]{28}$/);
    expect(await screen.findByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "uncertain");
    const stored = window.sessionStorage.getItem("eap.studio.toolRuns.v1");
    expect(stored).toContain(firstId);
    expect(stored).toContain(inputKey);
    expect(stored).not.toContain("Printer down");
    first.unmount();

    toolRunLedger.reloadFromStorage();
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.change(screen.getByLabelText("Subject"), { target: { value: "Printer down" } });
    expect(await screen.findByTestId("tool-run-reuse-hint")).toHaveAttribute("data-state", "uncertain");
    expect(toolRunLedger.pending("tickets.create", inputKey)?.runId).toBe(firstId);
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByTestId("tool-run-result");
    expect(vi.mocked(startToolRun).mock.calls[1][2]).toBe(firstId);
    expect(vi.mocked(startToolRun).mock.calls[1]).toHaveLength(3);
  });

  it("marks Form and JSON inside the Input mode group and notes a side-effecting tool", async () => {
    await renderTools();
    const group = screen.getByRole("group", { name: "Input mode" });
    expect(within(group).getByRole("button", { name: "Form" })).toHaveAttribute("aria-pressed", "true");
    expect(within(group).getByRole("button", { name: "JSON" })).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(within(group).getByRole("button", { name: "JSON" }));
    expect(within(group).getByRole("button", { name: "JSON" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/each Run performs it once/)).toBeInTheDocument();
  });
});
