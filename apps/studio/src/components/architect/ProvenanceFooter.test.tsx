import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { ProvenanceFooter } from "./ProvenanceFooter";
import { changedNotice, footerLine, shortHash, type Provenance, type ProvenanceRow } from "@/lib/provenance";

const row = (over: Partial<ProvenanceRow> = {}): ProvenanceRow => ({
  id: "p1", kind: "agent", definition_id: "reporter", spec_hash: "s".repeat(64), version: null, run_id: "r1", plan_id: null,
  created_at: "2026-11-23T10:00:00Z", card: "draft", platform_version: "0.1.0+a1b2c3d", manifest_version: 2,
  manifest_hash: "m".repeat(64), capability_snapshot_hash: "9f3c1a2b".padEnd(64, "0"), registry_version: "r".repeat(64),
  tool_calls: [{ name: "architect__list_capabilities", args_hash: "h", outcome: "ok" }, { name: "platform__list_agents", args_hash: "h2", outcome: "ok" }],
  degraded: [], truncated: [], model: "test:echo", ...over,
});
const data = (over: Partial<Provenance> = {}): Provenance => ({ definition_id: "reporter", kind: "agent", items: [row()], capabilities_changed: [], ...over });

describe("ProvenanceFooter", () => {
  test("one line: platform version and snapshot; expands to the manifest and the tool call names", async () => {
    render(<ProvenanceFooter kind="agent" definitionId="reporter" card="draft" load={vi.fn().mockResolvedValue(data())} />);
    expect(await screen.findByTestId("provenance-line")).toHaveTextContent("Based on: Platform 0.1.0+a1b2c3d · capabilities snapshot 9f3c1a2b");
    expect(screen.queryByTestId("provenance-details")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button"));
    const details = screen.getByTestId("provenance-details");
    expect(details).toHaveTextContent("Manifest version 2");
    expect(details).toHaveTextContent("architect__list_capabilities, platform__list_agents");
    expect(details).toHaveTextContent("Model test:echo");
  });

  test("the draft card shows how many capabilities changed since; the publish card does not", async () => {
    const changed = data({ capabilities_changed: [{ id: "tool.http.request", was: "available", now: "disabled" }] });
    const { unmount } = render(<ProvenanceFooter kind="agent" definitionId="reporter" card="draft" load={vi.fn().mockResolvedValue(changed)} />);
    expect(await screen.findByTestId("provenance-changed")).toHaveTextContent("1 capability changed since this was drafted: tool.http.request (available → disabled)");
    unmount();
    render(<ProvenanceFooter kind="agent" definitionId="reporter" card="publish" load={vi.fn().mockResolvedValue(changed)} />);
    await screen.findByTestId("provenance-footer-publish");
    expect(screen.queryByTestId("provenance-changed")).not.toBeInTheDocument();
  });

  test("renders nothing without provenance or when the read fails; it never blocks the card", async () => {
    const empty = render(<ProvenanceFooter kind="workflow" definitionId="w" card="draft" load={vi.fn().mockResolvedValue(data({ items: [] }))} />);
    await waitFor(() => expect(empty.container).toBeEmptyDOMElement());
    const failed = render(<ProvenanceFooter kind="workflow" definitionId="w" card="draft" load={vi.fn().mockRejectedValue(new Error("boom"))} />);
    await waitFor(() => expect(failed.container).toBeEmptyDOMElement());
  });
});

describe("provenance helpers", () => {
  test("short hashes, the footer line and the notice", () => {
    expect(shortHash("abcdef0123456789")).toBe("abcdef01");
    expect(shortHash(null)).toBe("unknown");
    expect(footerLine(row({ platform_version: null, capability_snapshot_hash: null }))).toBe("Platform unknown · capabilities snapshot unknown");
    expect(changedNotice([])).toBeNull();
    expect(changedNotice([{ id: "a", was: null, now: "available" }, { id: "b", was: "available", now: null }])).toBe("2 capabilities changed since this was drafted");
  });
});
