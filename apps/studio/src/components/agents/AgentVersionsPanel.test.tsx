import { describe, it, expect, vi, beforeEach } from "vitest";
import { chooseVersionAction, versionActions } from "@/components/agents/versionsTesting";
import React from "react";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { AgentVersionsPanel } from "./AgentVersionsPanel";

describe("AgentVersionsPanel", () => {
  it("does not render when no versions", () => {
    const { container } = render(<AgentVersionsPanel versions={[]} />);
    expect(container).toBeEmpty();
  });

  it("renders versions list", () => {
    const versions = [
      { agent_id: "1", version: 1, spec_hash: "abc12345def67890", created_at: "2026-11-11T10:00:00Z" },
      { agent_id: "1", version: 2, spec_hash: "xyz98765fed43210", created_at: "2026-11-11T11:00:00Z" },
    ];
    const { container } = render(<AgentVersionsPanel versions={versions} />);

    expect(container.innerHTML).toContain("Versions");
    expect(container.innerHTML).toContain("Version");
    expect(container.innerHTML).toContain("Hash");
    expect(container.innerHTML).toContain("Created");
    expect(container.innerHTML).toContain("abc12345");
    expect(container.innerHTML).toContain("Showing 2 versions");
  });
});

vi.mock("@/lib/agentVersions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/agentVersions")>();
  return { ...actual, compareVersions: vi.fn(), restoreVersion: vi.fn() };
});

import { fireEvent, waitFor, within } from "@testing-library/react";
import { ApiError } from "@/lib/api";
import { compareVersions, restoreVersion } from "@/lib/agentVersions";

describe("AgentVersionsPanel compare and restore", () => {
  const v = (n: number, restored_from: number | null = null) => ({
    agent_id: "a1",
    version: n,
    spec_hash: `hash${n}000000`,
    created_at: "2026-11-11T10:00:00Z",
    restored_from,
  });
  const versions = [v(4, 1), v(3), v(2), v(1)];
  const compareResult = {
    agent_id: "a1",
    base: { version: 1, spec: {}, spec_hash: "h1", created_at: null, created_by: null, restored_from: null },
    target: { version: 3, spec: {}, spec_hash: "h3", created_at: null, created_by: null, restored_from: null },
    identical: false,
    changes: [{ path: "model.name", kind: "changed", before: "a", after: "b" }],
    truncated: false,
    instructions_diff: [],
  };

  beforeEach(() => vi.clearAllMocks());

  it("shows restored from and hides Restore on the latest version", async () => {
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    expect(screen.getByText("restored from v1")).toBeInTheDocument();
    expect(await versionActions(4)).not.toContain("Restore this version");
    expect(await versionActions(1)).toContain("Restore this version");
  });

  it("read-only without agentId", () => {
    render(<AgentVersionsPanel versions={versions} />);
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByText("Restore this version")).not.toBeInTheDocument();
  });

  it("compares two selected versions, lower as base, and caps selection at two", async () => {
    vi.mocked(compareVersions).mockResolvedValue(compareResult as never);
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    const compareSelected = screen.getByRole("button", { name: "Compare selected" });
    expect(compareSelected).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select version 3 for compare" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select version 1 for compare" }));
    expect(screen.getByRole("checkbox", { name: "Select version 2 for compare" })).toBeDisabled();
    fireEvent.click(compareSelected);
    await waitFor(() => expect(compareVersions).toHaveBeenCalledWith("a1", 1, 3));
    expect(await screen.findByText("model.name")).toBeInTheDocument();
  });

  it("compares the draft with the latest version", async () => {
    vi.mocked(compareVersions).mockResolvedValue(compareResult as never);
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Compare draft with latest" }));
    await waitFor(() => expect(compareVersions).toHaveBeenCalledWith("a1", 4, "draft"));
  });

  it("restore asks for confirmation, states a new version is published, then calls onRestored", async () => {
    const created = { ...v(5, 1), spec: {}, created_by: "alice" };
    vi.mocked(restoreVersion).mockResolvedValueOnce(created as never);
    const onRestored = vi.fn();
    render(<AgentVersionsPanel versions={versions} agentId="a1" hasUnsavedEdits onRestored={onRestored} />);
    await chooseVersionAction(1, "Restore this version");
    const dialog = await screen.findByRole("dialog", { name: "Restore version 1?" });
    expect(dialog).toHaveTextContent("publishes a new version, 5, with the spec of version 1");
    expect(dialog).toHaveTextContent("unsaved edits in the editor will be lost");
    expect(restoreVersion).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore and publish" }));
    await waitFor(() => expect(onRestored).toHaveBeenCalledWith(created));
    expect(restoreVersion).toHaveBeenCalledWith("a1", 1, { discardDraft: false });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Version 5 published, restored from v1");
  });

  it("after Restore and publish, focus moves to the published notice, not the page body (browser check)", async () => {
    vi.mocked(restoreVersion).mockResolvedValueOnce({ ...v(5, 1), spec: {}, created_by: "alice" } as never);
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    await chooseVersionAction(1, "Restore this version");
    const dialog = await screen.findByRole("dialog", { name: "Restore version 1?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore and publish" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent("Version 5 published, restored from v1");
    expect(notice).toHaveFocus();
  });

  it("after a successful restore the confirm never returns focus to its opener, even one still on screen (browser re-check)", async () => {
    vi.mocked(restoreVersion).mockResolvedValueOnce({ ...v(5, 1), spec: {}, created_by: "alice" } as never);
    const { useVersionRestore } = await import("./AgentVersionsPanel");
    function SheetLike() {
      const restore = useVersionRestore({ agentId: "a1", latest: 4 });
      return (
        <>
          <button type="button" onClick={() => restore.start(1)}>Restore v1</button>
          {restore.notice ? <p role="status" data-restore-notice="" tabIndex={-1}>{restore.notice}</p> : null}
          {restore.dialog}
        </>
      );
    }
    render(<SheetLike />);
    const opener = screen.getByRole("button", { name: "Restore v1" });
    opener.focus();
    fireEvent.click(opener);
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Restore and publish" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(opener).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveFocus();
  });

  it("a cancelled confirm still returns focus to its opener", async () => {
    const { useVersionRestore } = await import("./AgentVersionsPanel");
    function SheetLike() {
      const restore = useVersionRestore({ agentId: "a1", latest: 4 });
      return (
        <>
          <button type="button" onClick={() => restore.start(1)}>Restore v1</button>
          {restore.dialog}
        </>
      );
    }
    render(<SheetLike />);
    const opener = screen.getByRole("button", { name: "Restore v1" });
    opener.focus();
    fireEvent.click(opener);
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(opener).toHaveFocus();
  });

  it("dirty-draft 409 offers discard draft and restore", async () => {
    vi.mocked(restoreVersion)
      .mockRejectedValueOnce(new ApiError(409, "the draft has unpublished changes since version 4; publish them, or restore with discard_draft: true to replace the draft"))
      .mockResolvedValueOnce({ ...v(5, 2), spec: {}, created_by: "alice" } as never);
    const onRestored = vi.fn();
    render(<AgentVersionsPanel versions={versions} agentId="a1" onRestored={onRestored} />);
    await chooseVersionAction(2, "Restore this version");
    fireEvent.click(await screen.findByRole("button", { name: "Restore and publish" }));
    const dialog = await screen.findByRole("dialog", { name: "Discard draft and restore version 2?" });
    expect(dialog).toHaveTextContent("unpublished changes since version 4");
    fireEvent.click(within(dialog).getByRole("button", { name: "Discard draft and restore" }));
    await waitFor(() => expect(onRestored).toHaveBeenCalled());
    expect(restoreVersion).toHaveBeenLastCalledWith("a1", 2, { discardDraft: true });
  });

  it.each([
    [409, "version 3 has the same spec as the latest version 4"],
    [409, "concurrent publish, retry"],
    [422, "Model test:gone is not in the allowlist"],
  ])("shows a %s error in the dialog: %s", async (status, message) => {
    vi.mocked(restoreVersion).mockRejectedValueOnce(new ApiError(status, message));
    const onRestored = vi.fn();
    render(<AgentVersionsPanel versions={versions} agentId="a1" onRestored={onRestored} />);
    await chooseVersionAction(3, "Restore this version");
    fireEvent.click(await screen.findByRole("button", { name: "Restore and publish" }));
    expect(await screen.findByTestId("confirm-dialog-error")).toHaveTextContent(message);
    expect(screen.getByRole("dialog", { name: "Restore version 3?" })).toBeInTheDocument();
    expect(onRestored).not.toHaveBeenCalled();
  });

  it("Cancel closes the dialog without restoring", async () => {
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    await chooseVersionAction(1, "Restore this version");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(restoreVersion).not.toHaveBeenCalled();
  });
});
