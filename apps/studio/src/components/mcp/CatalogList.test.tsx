import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CatalogList } from "./CatalogList";
import { LOCAL_ENTRY, REMOTE_ENTRY } from "./catalogFixtures";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
let fetchMock: ReturnType<typeof vi.fn>;
const urls = () => fetchMock.mock.calls.map((call) => String(call[0]));

describe("CatalogList", () => {
  beforeEach(() => {
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.searchParams.get("after") === "c1") return json({ items: [{ ...REMOTE_ENTRY, name: "io.example/more", title: "More" }], next_after: null });
      return json({ items: [LOCAL_ENTRY, REMOTE_ENTRY], next_after: "c1" });
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "dev-token");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.useRealTimers();
  });

  it("lists entries with badges, licence and registry status", async () => {
    render(<CatalogList onOpen={vi.fn()} />);
    expect(await screen.findByText("Files")).toBeInTheDocument();
    expect(screen.getByText("Read and write files.")).toBeInTheDocument();
    expect(screen.getByText("MIT")).toBeInTheDocument();
    expect(screen.getByText("deprecated")).toBeInTheDocument();
    expect(screen.getAllByText("Local").length).toBe(1);
    expect(screen.getAllByText("Remote").length).toBe(2);
    expect(urls()[0]).toContain("/api/v1/mcp-catalog");
  });

  it("debounces the search and sends q", async () => {
    render(<CatalogList onOpen={vi.fn()} debounceMs={20} />);
    await screen.findByText("Files");
    fireEvent.change(screen.getByRole("searchbox", { name: /search/i }), { target: { value: "fi" } });
    fireEvent.change(screen.getByRole("searchbox", { name: /search/i }), { target: { value: "files" } });
    await waitFor(() => expect(urls().some((url) => url.includes("q=files"))).toBe(true));
    expect(urls().some((url) => url.includes("q=fi&"))).toBe(false);
    expect(urls().filter((url) => url.includes("q=")).length).toBe(1);
  });

  it("loads more with next_after and appends", async () => {
    render(<CatalogList onOpen={vi.fn()} />);
    await screen.findByText("Files");
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("More")).toBeInTheDocument();
    expect(screen.getByText("Files")).toBeInTheDocument();
    expect(urls().some((url) => url.includes("after=c1"))).toBe(true);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("opens an entry", async () => {
    const onOpen = vi.fn();
    render(<CatalogList onOpen={onOpen} />);
    fireEvent.click(await screen.findByRole("button", { name: /Files/ }));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ name: "io.example/files" }));
  });

  it("shows a load error with retry", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ detail: "boom" }), { status: 500 }));
    render(<CatalogList onOpen={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Files")).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    const { container } = render(<CatalogList onOpen={vi.fn()} />);
    await screen.findByText("Files");
    await act(async () => undefined);
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
