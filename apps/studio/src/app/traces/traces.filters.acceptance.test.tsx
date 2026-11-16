// W2.2: the trace list's tag, metadata and text filters live in the URL, and the column chooser
// persists per viewer in localStorage (guarded), is an accessible menu with checkboxes, and the
// new sources pass the phone layout guard.
import fs from "node:fs";
import path from "node:path";
import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TracesPage from "./page";
import { listTraceRuns, parseTraceFilters, traceFiltersToParams } from "@/lib/traces";
import { api } from "@/lib/api";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { checkLayout } from "@/test/layoutGuard";

const nav = vi.hoisted(() => ({ query: "", replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/traces",
  useRouter: () => ({ push: vi.fn(), replace: nav.replace }),
  useSearchParams: () => new URLSearchParams(nav.query),
}));
vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/traces")>()), listTraceRuns: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { agents: { list: vi.fn() } },
}));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));

const run = {
  run_id: "run-1", kind: "agent", agent_id: "agent-1", version: 1, status: "completed", display_status: "completed",
  input: "in", output: "out", error: null, heartbeat_at: null, created_at: "2026-11-12T10:00:00Z", updated_at: "2026-11-12T10:00:00Z",
  trace_id: null, conversation_id: null, turn: null, tags: ["kind:agent", "ctx:customer"],
};
const list = vi.mocked(listTraceRuns) as unknown as ReturnType<typeof vi.fn>;
/** Radix opens a menu on Enter at its trigger (jsdom has no pointer events); items select on click. */
const openChooser = () => fireEvent.keyDown(screen.getByRole("button", { name: "Columns" }), { key: "Enter" });
const KEY = "studio.traces.hiddenColumns";

describe("trace list filters in the URL", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    nav.query = "";
    list.mockResolvedValue({ items: [run], next_before: null });
    vi.mocked(api.agents.list).mockResolvedValue([] as never);
  });

  it("parses and writes tags, metadata and q", () => {
    const f = parseTraceFilters(new URLSearchParams("tags=draft,kind:agent&metadata=customer%3DAda&metadata=bad&q=refund"));
    expect(f.tags).toEqual(["draft", "kind:agent"]);
    expect(f.metadata).toEqual(["customer=Ada"]);
    expect(f.q).toBe("refund");
    const params = traceFiltersToParams(f);
    expect(params.get("tags")).toBe("draft,kind:agent");
    expect(params.getAll("metadata")).toEqual(["customer=Ada"]);
    expect(params.get("q")).toBe("refund");
  });

  it("reads them from the URL into the controls and the list request, and writes changes back", async () => {
    nav.query = "tags=draft&metadata=customer%3DAda&q=refund";
    render(<TracesPage />);
    await waitFor(() => expect(list).toHaveBeenCalled());
    expect(list.mock.calls[0][0]).toMatchObject({ tags: ["draft"], metadata: ["customer=Ada"], q: "refund" });
    expect(screen.getByLabelText("Search")).toHaveValue("refund");
    expect(screen.getByLabelText("Tags")).toHaveValue("draft");
    expect(screen.getByLabelText("Metadata")).toHaveValue("customer=Ada");

    const tags = screen.getByLabelText("Tags");
    fireEvent.change(tags, { target: { value: "draft, ctx:region" } });
    fireEvent.keyDown(tags, { key: "Enter" });
    const url = nav.replace.mock.calls.at(-1)![0] as string;
    const params = new URLSearchParams(url.split("?")[1]);
    expect(params.get("tags")).toBe("draft,ctx:region");
    expect(params.getAll("metadata")).toEqual(["customer=Ada"]);
    expect(params.get("q")).toBe("refund");
  });

  it("says no runs match when only the new filters are set", async () => {
    nav.query = "q=zzz";
    list.mockResolvedValue({ items: [], next_before: null });
    render(<TracesPage />);
    expect(await screen.findByText("No runs match these filters.")).toBeInTheDocument();
  });

  it("the column chooser is a menu of checkboxes; hiding a column persists and survives a re-render", async () => {
    const { unmount } = render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    // Tags are off by default; Model is on.
    expect(screen.queryByTestId("trace-row-tags")).toBeNull();
    expect(screen.getByTestId("trace-row-model")).toBeInTheDocument();

    openChooser();
    const menu = await screen.findByRole("menu");
    const tagsBox = within(menu).getByRole("menuitemcheckbox", { name: "Tags" });
    const modelBox = within(menu).getByRole("menuitemcheckbox", { name: "Model" });
    expect(tagsBox).toHaveAttribute("aria-checked", "false");
    expect(modelBox).toHaveAttribute("aria-checked", "true");
    expect(within(menu).getByRole("menuitemcheckbox", { name: "Time" })).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(tagsBox);
    fireEvent.click(modelBox);
    await waitFor(() => expect(screen.getByTestId("trace-row-tags")).toHaveTextContent("kind:agent"));
    expect(screen.queryByTestId("trace-row-model")).toBeNull();
    expect(JSON.parse(window.localStorage.getItem(KEY)!)).toEqual(["model"]);
    unmount();

    render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    expect(screen.queryByTestId("trace-row-model")).toBeNull();
    expect(screen.getByTestId("trace-row-tags")).toBeInTheDocument();
  });

  it("works when localStorage throws", async () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    openChooser();
    fireEvent.click(await screen.findByRole("menuitemcheckbox", { name: "Tags" }));
    expect(await screen.findByTestId("trace-row-tags")).toBeInTheDocument();
    get.mockRestore();
    set.mockRestore();
  });

  it("has no axe violations with the chooser open", async () => {
    const { container } = render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    openChooser();
    await screen.findByRole("menu");
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);

  it("the new sources pass the phone (375 px) layout guard", () => {
    const root = path.resolve(__dirname, "../..");
    for (const file of ["app/traces/page.tsx", "components/shared/ColumnChooser.tsx", "components/traces/TraceFeedback.tsx", "components/traces/AddToDataset.tsx"]) {
      expect(checkLayout(file, fs.readFileSync(path.join(root, file), "utf8"))).toEqual([]);
    }
  });
});
