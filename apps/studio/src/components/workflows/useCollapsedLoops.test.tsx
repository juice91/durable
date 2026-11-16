import React from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ replace: vi.fn(), search: new URLSearchParams("tab=runs&collapsed=outer,inner,BAD") }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: vi.fn() }),
  useSearchParams: () => nav.search,
  usePathname: () => "/workflows/wf-1",
}));

import { useCollapsedLoops, type CollapsedLoops } from "./useCollapsedLoops";

afterEach(cleanup);

describe("useCollapsedLoops", () => {
  it("reads ?collapsed=, drops non-ids, and writes a sorted set with replace, keeping other params", () => {
    let api: CollapsedLoops | null = null;
    function Probe() {
      api = useCollapsedLoops();
      return <span data-testid="probe">{[...api.collapsed].join("|")}</span>;
    }
    render(<Probe />);
    expect(screen.getByTestId("probe")).toHaveTextContent("outer|inner");
    act(() => api!.setCollapsed(new Set(["zeta", "each"])));
    expect(screen.getByTestId("probe")).toHaveTextContent("zeta|each");
    expect(nav.replace).toHaveBeenCalledWith("/workflows/wf-1?tab=runs&collapsed=each%2Czeta", { scroll: false });
    act(() => api!.setCollapsed(new Set()));
    expect(nav.replace).toHaveBeenLastCalledWith("/workflows/wf-1?tab=runs", { scroll: false });
  });
});
