import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import { ApiError } from "@/lib/api";
import { compareVersions, type VersionCompareResult } from "@/lib/agentVersions";
import { VersionCompare, VersionCompareView, sideBySideRows } from "./VersionCompare";

vi.mock("@/lib/agentVersions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/agentVersions")>();
  return { ...actual, compareVersions: vi.fn() };
});

const spec = { spec_version: 1 as const, name: "A", instructions: "", model: { provider: "test", name: "echo" }, tools: [] };

function result(overrides: Partial<VersionCompareResult> = {}): VersionCompareResult {
  return {
    agent_id: "a1",
    base: { version: 1, spec, spec_hash: "h1", created_at: "2026-11-11T00:00:00Z", created_by: "alice", restored_from: null },
    target: { version: 3, spec, spec_hash: "h3", created_at: "2026-11-12T00:00:00Z", created_by: "alice", restored_from: null },
    identical: false,
    changes: [
      { path: "model.name", kind: "changed", before: "echo", after: "ticket" },
      { path: "tools[0]", kind: "added", before: null, after: "lookup" },
      { path: "instructions", kind: "changed", before: "x", after: "y" },
    ],
    truncated: false,
    instructions_diff: [
      { op: "equal", text: "You are helpful.", base_line: 1, target_line: 1 },
      { op: "removed", text: "Be brief.", base_line: 2, target_line: null },
      { op: "added", text: "Be thorough.", base_line: null, target_line: 2 },
      { op: "added", text: "Cite sources.", base_line: null, target_line: 3 },
    ],
    ...overrides,
  };
}

describe("sideBySideRows", () => {
  it("pairs removed and added runs and keeps equal lines on both sides", () => {
    const rows = sideBySideRows(result().instructions_diff);
    expect(rows).toHaveLength(3);
    expect(rows[0].base?.text).toBe("You are helpful.");
    expect(rows[0].target?.text).toBe("You are helpful.");
    expect(rows[1].base?.text).toBe("Be brief.");
    expect(rows[1].target?.text).toBe("Be thorough.");
    expect(rows[2].base).toBeNull();
    expect(rows[2].target?.text).toBe("Cite sources.");
  });
});

describe("VersionCompareView", () => {
  it("lists changed fields (instructions only in the line diff) and the side-by-side diff", () => {
    render(<VersionCompareView result={result()} />);
    const rows = screen.getAllByTestId("compare-change");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByRole("rowheader")).toHaveTextContent("model.name");
    expect(within(rows[0]).getByText("Changed")).toBeInTheDocument();
    expect(within(rows[0]).getByText("echo")).toBeInTheDocument();
    expect(within(rows[0]).getByText("ticket")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Added")).toBeInTheDocument();
    expect(within(rows[1]).getByText("—")).toBeInTheDocument();

    const diff = screen.getByTestId("instructions-diff");
    expect(within(diff).getByRole("columnheader", { name: "v1" })).toBeInTheDocument();
    expect(within(diff).getByRole("columnheader", { name: "v3" })).toBeInTheDocument();
    expect(within(diff).getByText("removed:")).toBeInTheDocument();
    expect(within(diff).getAllByText("added:")).toHaveLength(2);
  });

  it("says when the specs are identical", () => {
    render(<VersionCompareView result={result({ identical: true, changes: [], instructions_diff: [] })} />);
    expect(screen.getByRole("status")).toHaveTextContent("v1 and v3 have the same spec.");
  });

  it("labels the draft side and notes truncation", () => {
    const r = result({ truncated: true, instructions_diff: [{ op: "equal", text: "same", base_line: 1, target_line: 1 }] });
    r.target = { ...r.target, version: "draft", spec_hash: null, created_at: null, created_by: null };
    render(<VersionCompareView result={r} />);
    expect(screen.getByRole("columnheader", { name: "Draft" })).toBeInTheDocument();
    expect(screen.getByText(/cut short/)).toBeInTheDocument();
    expect(screen.getByText("No changes to the instructions.")).toBeInTheDocument();
  });

  it("says the diff is too large when instructions_diff_truncated is set, above the coarse block replace", () => {
    render(
      <VersionCompareView
        result={result({
          instructions_diff_truncated: true,
          instructions_diff: [
            { op: "removed", text: "old 1", base_line: 1, target_line: null },
            { op: "added", text: "new 1", base_line: null, target_line: 1 },
          ],
        })}
      />,
    );
    expect(screen.getByTestId("instructions-diff-truncated")).toHaveTextContent("Diff too large to show line by line");
    expect(screen.getByTestId("instructions-diff")).toBeInTheDocument();
  });

  it("shows only the notice when the truncated diff is empty, and nothing when the field is absent", () => {
    const { unmount } = render(<VersionCompareView result={result({ instructions_diff_truncated: true, instructions_diff: [] })} />);
    expect(screen.getByTestId("instructions-diff-truncated")).toHaveTextContent("Diff too large to show line by line");
    expect(screen.queryByTestId("instructions-diff")).not.toBeInTheDocument();
    expect(screen.queryByText("No changes to the instructions.")).not.toBeInTheDocument();
    unmount();
    render(<VersionCompareView result={result()} />);
    expect(screen.queryByTestId("instructions-diff-truncated")).not.toBeInTheDocument();
  });
});

describe("VersionCompare", () => {
  beforeEach(() => vi.clearAllMocks());

  it("fetches the compare and renders it, Close calls onClose", async () => {
    vi.mocked(compareVersions).mockResolvedValueOnce(result());
    const onClose = vi.fn();
    render(<VersionCompare agentId="a1" base={1} target={3} onClose={onClose} />);
    expect(screen.getByRole("region", { name: "Compare v1 with v3" })).toBeInTheDocument();
    expect(await screen.findByTestId("instructions-diff")).toBeInTheDocument();
    expect(compareVersions).toHaveBeenCalledWith("a1", 1, 3);
    fireEvent.click(screen.getByRole("button", { name: "Close compare" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("shows the server error", async () => {
    vi.mocked(compareVersions).mockRejectedValueOnce(new ApiError(404, "Version 9 not found"));
    render(<VersionCompare agentId="a1" base={9} target="draft" onClose={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Version 9 not found");
  });
});
