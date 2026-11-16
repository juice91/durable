// LP.12 review: the iteration pagers' URL state, the `?collapsed=` pattern of LP.6.
import React from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ replace: vi.fn(), search: new URLSearchParams("collapsed=each&iter=each.4,outer.1/inner.0,BAD&failedOnly=each") }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: vi.fn() }),
  useSearchParams: () => nav.search,
  usePathname: () => "/workflows/wf-1/runs/r1",
}));

import { useRunIterations, type RunIterations } from "./useRunIterations";

afterEach(cleanup);

describe("useRunIterations", () => {
  it("reads ?iter= and ?failedOnly=, and writes both with replace in one tick, keeping other params", () => {
    let api: RunIterations | null = null;
    function Probe() {
      api = useRunIterations();
      return <span data-testid="probe">{JSON.stringify(api.selection)}|{[...api.failedOnly].join(",")}</span>;
    }
    render(<Probe />);
    expect(screen.getByTestId("probe")).toHaveTextContent('{"each":4,"outer.1/inner":0}|each');
    act(() => {
      // "Failed only" also moves the pager: two writes before the router reports the URL.
      api!.setFailedOnly(new Set(["each", "outer.1/inner"]));
      api!.setSelection({ each: 1 });
    });
    expect(screen.getByTestId("probe")).toHaveTextContent('{"each":1}|each,outer.1/inner');
    const last = new URL(String(nav.replace.mock.calls.at(-1)![0]), "http://studio.test");
    expect(last.pathname).toBe("/workflows/wf-1/runs/r1");
    expect(Object.fromEntries(last.searchParams)).toEqual({ collapsed: "each", iter: "each.1", failedOnly: "each,outer.1/inner" });
    expect(nav.replace.mock.calls.at(-1)![1]).toEqual({ scroll: false });
  });
});
