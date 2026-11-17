// The Agents page lists one Architect: the drafter helpers are system agents, shown on request.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

vi.mock("next/navigation", () => ({
  usePathname: () => "/agents",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(""),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { agents: { list: vi.fn(), listArchived: vi.fn() } } };
});
vi.mock("@/components/shortcuts/ShortcutsProvider", () => ({ useCreateAction: () => undefined }));

import { api, type Agent } from "@/lib/api";
import AgentsPage from "./page";

const row = (over: Partial<Agent>): Agent => ({ id: "x", name: "x", latest_version: 1, draft_spec: { name: "x", model: { provider: "t", name: "m" } }, ...over }) as Agent;
const architect = row({ id: "a", name: "Architect", builtin: true, builtin_role: "architect" });
const drafter = row({ id: "d", name: "Architect drafter", builtin: true, builtin_role: "drafter" });

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  vi.mocked(api.agents.list).mockImplementation((_init, options) => Promise.resolve(options?.includeSystem ? [architect, drafter] : [architect]));
});
afterEach(cleanup);

describe("Agents page system agents", () => {
  it("lists only the Architect by default and asks for system agents on the toggle, remembering the choice", async () => {
    render(<AgentsPage />);
    expect((await screen.findAllByText("Architect")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Architect drafter")).toBeNull();
    expect(vi.mocked(api.agents.list).mock.calls.every(([, o]) => !o?.includeSystem)).toBe(true);

    fireEvent.click(screen.getByLabelText("Show system agents"));
    expect(await screen.findByText("Architect drafter")).toBeInTheDocument();
    expect(vi.mocked(api.agents.list).mock.calls.at(-1)?.[1]).toEqual({ includeSystem: true });
    expect(window.localStorage.getItem("studio.agents.show-system")).toBe("true");
  });

  it("starts with system agents shown when the viewer chose that before", async () => {
    window.localStorage.setItem("studio.agents.show-system", "true");
    render(<AgentsPage />);
    expect(await screen.findByText("Architect drafter")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Show system agents")).toBeChecked());
  });

  it("still works when localStorage throws", async () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    render(<AgentsPage />);
    expect((await screen.findAllByText("Architect")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByLabelText("Show system agents"));
    expect(await screen.findByText("Architect drafter")).toBeInTheDocument();
    spy.mockRestore();
    set.mockRestore();
  });
});
