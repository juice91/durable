import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { CreatedList } from "./CreatedList";
import type { CreatedItem } from "@/lib/architect";

const item = (id: string, name: string, extra: Partial<CreatedItem> = {}): CreatedItem => ({
  id, name, versions: 2, latest_created_via: null, draft: false, archived: false, ...extra,
});
const agents = [item("agent-9a54", "球棒"), item("definition", "definition", { versions: 0, draft: true, archived: true })];
const workflows = [item("agent-b50e", "球棒棒球貼文")];

function mount() {
  return render(<CreatedList agents={agents} workflows={workflows} selectedAgentId={null} onNew={() => {}} onSelectAgent={() => {}} />);
}

describe("CreatedList", () => {
  beforeEach(() => window.localStorage.clear());

  test("shows the name with id and version beneath; archived hidden by default", () => {
    mount();
    expect(screen.getByText("球棒")).toBeInTheDocument();
    expect(screen.getByText("agent-9a54 · v2")).toBeInTheDocument();
    expect(screen.getByText("球棒棒球貼文")).toBeInTheDocument();
    expect(screen.queryByText("definition · v0")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show archived (1)" })).toBeInTheDocument();
  });

  test("the toggle reveals archived items with badges and is remembered", async () => {
    const first = mount();
    fireEvent.click(screen.getByRole("button", { name: "Show archived (1)" }));
    expect(screen.getByText("definition · v0")).toBeInTheDocument();
    expect(screen.getByText("Archived")).toBeInTheDocument();
    expect(screen.getByTestId("architect-draft-marker")).toBeInTheDocument();
    first.unmount();
    mount();
    expect(await screen.findByText("definition · v0")).toBeInTheDocument();
  });

  test("works when localStorage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Show archived (1)" }));
    expect(screen.getByText("definition · v0")).toBeInTheDocument();
    vi.restoreAllMocks();
  });
});
