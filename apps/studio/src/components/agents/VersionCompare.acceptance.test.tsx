import React from "react";
import { chooseVersionAction, versionActions } from "@/components/agents/versionsTesting";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AgentVersionsPanel } from "./AgentVersionsPanel";

vi.mock("@/lib/agentVersions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/agentVersions")>();
  return { ...actual, compareVersions: vi.fn(), restoreVersion: vi.fn() };
});

import { compareVersions, restoreVersion } from "@/lib/agentVersions";
import { ApiError } from "@/lib/api";

const v = (n: number, restored_from: number | null = null) => ({
  agent_id: "a1",
  version: n,
  spec_hash: `hash${n}000000`,
  created_at: "2026-11-11T10:00:00Z",
  restored_from,
});

const spec = { spec_version: 1, name: "A", instructions: "Help", model: { provider: "test", name: "echo" }, tools: [] };

describe("AgentVersionsPanel restore flow", () => {
  const versions = [v(4, 1), v(3), v(2), v(1)];

  beforeEach(() => vi.clearAllMocks());

  it("restore asks for confirmation and publishes a new version", async () => {
    const created = { ...v(5, 1), spec, created_by: "alice" };
    vi.mocked(restoreVersion).mockResolvedValueOnce(created as never);
    const onRestored = vi.fn();
    render(<AgentVersionsPanel versions={versions} agentId="a1" hasUnsavedEdits onRestored={onRestored} />);
    await chooseVersionAction(1, "Restore this version");
    const dialog = await screen.findByRole("dialog", { name: "Restore version 1?" });
    expect(dialog).toHaveTextContent("publishes a new version, 5, with the spec of version 1");
    expect(restoreVersion).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore and publish" }));
    await waitFor(() => expect(onRestored).toHaveBeenCalledWith(created));
    expect(restoreVersion).toHaveBeenCalledWith("a1", 1, { discardDraft: false });
  });

  it("dirty-draft 409 offers discard draft and restore", async () => {
    vi.mocked(restoreVersion)
      .mockRejectedValueOnce(new ApiError(409, "the draft has unpublished changes since version 4; publish them, or restore with discard_draft: true to replace the draft"))
      .mockResolvedValueOnce({ ...v(5, 2), spec, created_by: "alice" } as never);
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

  it("shows a same-spec 409 inside the dialog", async () => {
    vi.mocked(restoreVersion).mockRejectedValueOnce(new ApiError(409, "version 3 has the same spec as the latest version 4"));
    const onRestored = vi.fn();
    render(<AgentVersionsPanel versions={versions} agentId="a1" onRestored={onRestored} />);
    await chooseVersionAction(3, "Restore this version");
    fireEvent.click(await screen.findByRole("button", { name: "Restore and publish" }));
    expect(await screen.findByTestId("confirm-dialog-error")).toHaveTextContent("version 3 has the same spec as the latest version 4");
    expect(screen.getByRole("dialog", { name: "Restore version 3?" })).toBeInTheDocument();
    expect(onRestored).not.toHaveBeenCalled();
  });

  it("shows a 422 error for model not in allowlist", async () => {
    vi.mocked(restoreVersion).mockRejectedValueOnce(new ApiError(422, "Model test:gone is not in the allowlist"));
    const onRestored = vi.fn();
    render(<AgentVersionsPanel versions={versions} agentId="a1" onRestored={onRestored} />);
    await chooseVersionAction(3, "Restore this version");
    fireEvent.click(await screen.findByRole("button", { name: "Restore and publish" }));
    expect(await screen.findByTestId("confirm-dialog-error")).toHaveTextContent("Model test:gone is not in the allowlist");
  });

  it("Cancel closes the dialog without restoring", async () => {
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    await chooseVersionAction(1, "Restore this version");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(restoreVersion).not.toHaveBeenCalled();
  });

  it("shows restored_from in the version row", () => {
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    expect(screen.getByText("restored from v1")).toBeInTheDocument();
  });

  it("hides Restore button on the latest version", async () => {
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    expect(await versionActions(4)).not.toContain("Restore this version");
    expect(await versionActions(1)).toContain("Restore this version");
  });
});

describe("AgentVersionsPanel compare", () => {
  const versions = [v(3), v(2), v(1)];
  const compareResult = {
    agent_id: "a1",
    base: { version: 1, spec, spec_hash: "h1", created_at: "2026-11-11T10:00:00Z", created_by: "alice", restored_from: null },
    target: { version: 3, spec, spec_hash: "h3", created_at: "2026-11-11T12:00:00Z", created_by: "alice", restored_from: null },
    identical: false,
    changes: [{ path: "model.name", kind: "changed", before: "echo", after: "ticket" }],
    truncated: false,
    instructions_diff: [],
  };

  beforeEach(() => vi.clearAllMocks());

  it("compares two selected versions", async () => {
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
    vi.mocked(compareVersions).mockResolvedValue({ ...compareResult, target: { ...compareResult.target, version: "draft" } } as never);
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Compare draft with latest" }));
    await waitFor(() => expect(compareVersions).toHaveBeenCalledWith("a1", 3, "draft"));
  });

  it("caps selection at two versions", async () => {
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    fireEvent.click(screen.getByRole("checkbox", { name: "Select version 3 for compare" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select version 1 for compare" }));
    expect(screen.getByRole("checkbox", { name: "Select version 2 for compare" })).toBeDisabled();
  });
});

describe("AgentVersionsPanel read-only", () => {
  const versions = [v(2), v(1)];

  it("does not render compare/restore without agentId", () => {
    render(<AgentVersionsPanel versions={versions} />);
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restore this version" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Compare selected" })).not.toBeInTheDocument();
  });
});

describe("AgentVersionsPanel compare results", () => {
  const versions = [v(3), v(2), v(1)];
  const side = (n: number) => ({ version: n, spec, spec_hash: `h${n}`, created_at: "2026-11-11T10:00:00Z", created_by: "alice", restored_from: null });

  beforeEach(() => vi.clearAllMocks());

  it("shows removed and added markers side by side for changed instructions", async () => {
    vi.mocked(compareVersions).mockResolvedValue({
      agent_id: "a1", base: side(1), target: side(3), identical: false, truncated: false, changes: [],
      instructions_diff: [
        { op: "equal", text: "Keep.", base_line: 1, target_line: 1 },
        { op: "removed", text: "Old line.", base_line: 2, target_line: null },
        { op: "added", text: "New line.", base_line: null, target_line: 2 },
      ],
    } as never);
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Compare draft with latest" }));
    const diff = await screen.findByTestId("instructions-diff");
    expect(within(diff).getByText("removed:")).toBeInTheDocument();
    expect(within(diff).getByText("added:")).toBeInTheDocument();
    expect(within(diff).getByText("Old line.")).toBeInTheDocument();
    expect(within(diff).getByText("New line.")).toBeInTheDocument();
  });

  it("says the specs are identical", async () => {
    vi.mocked(compareVersions).mockResolvedValue({
      agent_id: "a1", base: side(1), target: side(3), identical: true, truncated: false, changes: [], instructions_diff: [],
    } as never);
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Compare draft with latest" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("have the same spec"));
  });

  it("shows the server error when the compare fails", async () => {
    vi.mocked(compareVersions).mockRejectedValue(new ApiError(404, "version 9 not found"));
    render(<AgentVersionsPanel versions={versions} agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Compare draft with latest" }));
    expect(await screen.findByText(/version 9 not found/)).toBeInTheDocument();
  });
});
