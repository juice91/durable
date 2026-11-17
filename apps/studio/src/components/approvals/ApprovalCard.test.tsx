import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const wfMock = vi.hoisted(() => ({ decideApproval: vi.fn() }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));

import { ApprovalCard } from "./ApprovalCard";
const step = { kind: "step" as const, runId: "run-1", stepId: "gate", prompt: "?", deadline: null };

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

beforeEach(() => {
  wfMock.decideApproval.mockReset();
  wfMock.decideApproval.mockResolvedValue({ run_id: "run-1", step_id: "gate", decision: "approved", decided_by: "dev", comment: null, notified: true });
});
afterEach(() => cleanup());

describe("ApprovalCard pause", () => {
  it("titles a pause approval and offers Continue instead of Approve", () => {
    render(<ApprovalCard approval={{ ...step, stepId: "classify_pause", prompt: "Paused after classify", pause: true }} onRefresh={vi.fn()} />);
    expect(screen.getByRole("heading", { name: /Paused after step/ })).toHaveTextContent("classify");
    expect(screen.getByRole("button", { name: "Continue" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });
});

describe("ApprovalCard", () => {
  it("stays disabled after a decision until the refresh finishes", async () => {
    const refresh = deferred();
    const onRefresh = vi.fn(() => refresh.promise);
    render(<ApprovalCard approval={step} onRefresh={onRefresh} />);
    const approve = screen.getByRole("button", { name: "Approve" });
    fireEvent.click(approve);
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));

    // Decision recorded, refresh still in flight: a second click must not reach the API.
    expect(approve).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    fireEvent.click(approve);
    expect(wfMock.decideApproval).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("approval-error-gate")).toBeNull();

    await act(async () => {
      refresh.resolve();
      await refresh.promise;
    });
    expect(approve).toBeEnabled();
  });

  it("does not update state after it unmounts mid-refresh", async () => {
    const refresh = deferred();
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { unmount } = render(<ApprovalCard approval={step} onRefresh={() => refresh.promise} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(wfMock.decideApproval).toHaveBeenCalledTimes(1));
    unmount();
    await act(async () => {
      refresh.resolve();
      await refresh.promise;
    });
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });

  it("still accepts a void onRefresh", async () => {
    const onRefresh = vi.fn();
    render(<ApprovalCard approval={step} onRefresh={onRefresh} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
  });
});
