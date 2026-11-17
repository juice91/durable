import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { setWindowWidth } from "@/components/frame/frameTesting";
import { routes } from "@/lib/routes";
import { ThreadsPage } from "./ThreadsPage";

const nav = vi.hoisted(() => ({ search: "", replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/threads",
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));

const T = "2026-11-16T10:00:00Z";
const conv = (n: number, over: Record<string, unknown> = {}) => ({
  id: `c${n}`, agent_id: "a1", resource_id: "alice", title: `Thread ${n}`, turn_count: n, last_turn_run_id: null,
  last_turn_status: null, last_turn_display_status: null, created_at: T, updated_at: T, ...over,
});
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

type Call = { method: string; url: URL };
let calls: Call[];
let handlers: Record<string, (call: Call) => Response>;

beforeEach(() => {
  calls = [];
  nav.search = "";
  nav.replace.mockReset();
  handlers = {
    "GET /agents": () => json([{ id: "a1", name: "Support bot" }, { id: "a2", name: "Other bot" }]),
    "GET /conversations": () => json({ items: [conv(2), conv(1, { agent_id: "a2", resource_id: null })], next_before: null }),
  };
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    const call = { method, url };
    calls.push(call);
    const handler = handlers[`${method} ${url.pathname.replace("/api/v1", "")}`];
    return handler ? handler(call) : json({ detail: "missing" }, 404);
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("Threads page", () => {
  it("lists conversations across agents and opens one in the agent's chat", async () => {
    render(<ThreadsPage />);
    const rows = await screen.findAllByTestId("thread-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Thread 2")).toBeInTheDocument();
    await waitFor(() => expect(within(rows[0]).getByText("Support bot")).toBeInTheDocument());
    expect(within(rows[1]).getByText("Other bot")).toBeInTheDocument();
    expect(within(rows[0]).getByText("alice")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("link", { name: /Thread 2/ })).toHaveAttribute("href", routes.agentChat("a1", { conversation: "c2" }));
    expect(within(rows[1]).getByRole("link", { name: /Thread 1/ })).toHaveAttribute("href", routes.agentChat("a2", { conversation: "c1" }));
  });

  it("reads the filters from the URL and sends them to the API", async () => {
    nav.search = "agent_id=a2&q=refund";
    render(<ThreadsPage />);
    await screen.findAllByTestId("thread-row");
    const list = calls.find((c) => c.url.pathname.endsWith("/conversations"))!;
    expect(list.url.searchParams.get("agent_id")).toBe("a2");
    expect(list.url.searchParams.get("q")).toBe("refund");
    expect(screen.getByLabelText("Agent")).toHaveValue("a2");
    expect(screen.getByRole("searchbox")).toHaveValue("refund");
  });

  it("writes an agent choice and a search to the URL", async () => {
    render(<ThreadsPage />);
    await screen.findAllByTestId("thread-row");
    await waitFor(() => expect(screen.getByRole("option", { name: "Other bot" })).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "a2" } });
    expect(nav.replace).toHaveBeenLastCalledWith("/threads?agent_id=a2");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "refund" } });
    await waitFor(() => expect(nav.replace).toHaveBeenLastCalledWith("/threads?q=refund"), { timeout: 2000 });
  });

  it("loads further pages", async () => {
    const seen: (string | null)[] = [];
    handlers["GET /conversations"] = ({ url }) => {
      seen.push(url.searchParams.get("before"));
      return url.searchParams.get("before") ? json({ items: [conv(2)], next_before: null }) : json({ items: [conv(1)], next_before: "cur" });
    };
    render(<ThreadsPage />);
    await screen.findByText("Thread 1");
    fireEvent.click(screen.getByRole("button", { name: "Load more threads" }));
    await screen.findByText("Thread 2");
    expect(seen).toEqual([null, "cur"]);
  });

  it("renames a conversation", async () => {
    handlers["PATCH /conversations/c2"] = () => json(conv(2, { title: "Renamed" }));
    render(<ThreadsPage />);
    const row = (await screen.findAllByTestId("thread-row"))[0];
    fireEvent.keyDown(within(row).getByTestId("thread-actions"), { key: "Enter" });
    fireEvent.click(await screen.findByTestId("thread-rename"));
    const input = await screen.findByLabelText("Title");
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH" && c.url.pathname.endsWith("/conversations/c2"))).toBe(true));
    const patch = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.find((c) => c[1]?.method === "PATCH");
    expect(JSON.parse(patch![1].body)).toEqual({ title: "Renamed" });
  });

  it("clones a conversation", async () => {
    handlers["POST /conversations/c2/clone"] = () => json(conv(3, { title: "Thread 2 (copy)" }), 201);
    render(<ThreadsPage />);
    const row = (await screen.findAllByTestId("thread-row"))[0];
    fireEvent.keyDown(within(row).getByTestId("thread-actions"), { key: "Enter" });
    fireEvent.click(await screen.findByTestId("thread-clone"));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.pathname.endsWith("/conversations/c2/clone"))).toBe(true));
  });

  it("deletes a conversation after confirmation", async () => {
    handlers["DELETE /conversations/c2"] = () => new Response(null, { status: 204 });
    render(<ThreadsPage />);
    const row = (await screen.findAllByTestId("thread-row"))[0];
    fireEvent.keyDown(within(row).getByTestId("thread-actions"), { key: "Enter" });
    fireEvent.click(await screen.findByTestId("thread-delete"));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    fireEvent.click(await screen.findByRole("button", { name: "Delete conversation" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url.pathname.endsWith("/conversations/c2"))).toBe(true));
  });

  it("has empty, filtered-empty and error states", async () => {
    handlers["GET /conversations"] = () => json({ items: [], next_before: null });
    const first = render(<ThreadsPage />);
    expect(await screen.findByText("No threads yet")).toBeInTheDocument();
    first.unmount();
    nav.search = "agent_id=a2";
    const second = render(<ThreadsPage />);
    expect(await screen.findByText("No threads match")).toBeInTheDocument();
    second.unmount();
    nav.search = "q=zzz";
    const third = render(<ThreadsPage />);
    expect(await screen.findByTestId("datalist-no-match")).toHaveTextContent("No threads match");
    third.unmount();
    nav.search = "";
    nav.search = "";
    let fail = true;
    handlers["GET /conversations"] = () => (fail ? json({ detail: "boom" }, 500) : json({ items: [conv(1)], next_before: null }));
    render(<ThreadsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(await screen.findByText("Thread 1")).toBeInTheDocument();
  });

  it("fits 375 px and has no axe violations", async () => {
    const restore = setWindowWidth(375);
    const { container } = render(<main><ThreadsPage /></main>);
    await screen.findByText("Thread 2");
    await expectNoAxeViolations(container);
    restore();
  }, AXE_TIMEOUT_MS);
});
