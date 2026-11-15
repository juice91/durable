/** AC.15 acceptance: 375 px fit, axe in both themes inside the frame, keyboard. */
import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { nextRouter } from "@/test/nextRouterMock";
import { resetPendingApprovalsForTests } from "@/lib/approvals";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { FrameHarness, mockMatchMedia, setWindowWidth, type MatchMediaControl } from "@/components/frame/frameTesting";
import ApprovalsPage from "./page";
import { AGENT, RUN, decidedTool, jsonResponse, stepItem, toolItem } from "./approvalsAcceptanceKit";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

const fetchMock = vi.fn();
let media: MatchMediaControl;
let restoreWidth: (() => void) | undefined;

beforeEach(() => {
  nextRouter.reset(["/approvals"]);
  resetPendingApprovalsForTests();
  media = mockMatchMedia(true);
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/agents")) return jsonResponse([{ id: AGENT, name: "Support bot" }]);
    if (url.includes("/runs/")) return jsonResponse({ id: RUN, conversation_id: "CONV1" });
    if (url.includes("status=decided")) return jsonResponse({ items: [decidedTool], next_cursor: null });
    return jsonResponse({ items: [toolItem, stepItem], next_cursor: null });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPendingApprovalsForTests();
  restoreWidth?.();
  restoreWidth = undefined;
  media.restore();
  localStorage.removeItem("studio.theme");
  document.documentElement.classList.remove("dark");
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("375 px", () => {
  it("the list scrolls in its own wrapper, the filter bar wraps, the Sheet is full width", async () => {
    restoreWidth = setWindowWidth(375);
    media.setWide(false);
    nextRouter.reset([routes.approval(RUN, "call_1")]);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    expect(sheet.style.width).toBe("");
    expect(screen.getByTestId("approvals-filter-bar").className).toContain("flex-wrap");
    expect(screen.getByTestId("approvals-list").querySelector("table")!.closest(".overflow-x-auto")).not.toBeNull();
    for (const el of document.body.querySelectorAll<HTMLElement>("[style]")) {
      const px = /(?:^|;|\s)width:\s*(\d+)px/.exec(el.getAttribute("style") ?? "");
      if (px) expect(Number(px[1])).toBeLessThanOrEqual(375);
    }
    await within(sheet).findByRole("button", { name: "Approve" });
  });
});

describe.each(["light", "dark"] as const)("axe inside the frame (%s)", (theme) => {
  beforeEach(() => {
    restoreWidth = setWindowWidth(1440);
    localStorage.setItem("studio.theme", theme);
    document.documentElement.classList.toggle("dark", theme === "dark");
  });
  it("the pending list, the decided list and an open Sheet have no violations and one h1", async () => {
    render(<FrameHarness><ApprovalsPage /></FrameHarness>);
    await screen.findByTestId(`approval-row-${RUN}:call_1`);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    await expectNoAxeViolations(document.body);
    fireEvent.click(screen.getByRole("radio", { name: "Decided" }));
    await act(() => nextRouter.settle());
    await screen.findByTestId(`approval-row-${RUN}:call_d`);
    await expectNoAxeViolations(document.body);
    fireEvent.click(within(screen.getByTestId(`approval-row-${RUN}:call_d`)).getByRole("button"));
    await act(() => nextRouter.settle());
    await screen.findByTestId("approval-sheet");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  it("the Sheet of a pending item (the decision card) has no violations", async () => {
    nextRouter.reset([routes.approval(RUN, "call_1")]);
    render(<FrameHarness><ApprovalsPage /></FrameHarness>);
    const sheet = await screen.findByTestId("approval-sheet");
    await within(sheet).findByRole("button", { name: "Approve" });
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});

describe("keyboard", () => {
  it("Enter on a row's button opens the Sheet and Escape closes it", async () => {
    render(<ApprovalsPage />);
    const button = within(await screen.findByTestId(`approval-row-${RUN}:call_1`)).getByRole("button");
    button.focus();
    fireEvent.click(button);
    await act(() => nextRouter.settle());
    const sheet = await screen.findByTestId("approval-sheet");
    expect(sheet).toHaveAttribute("role", "dialog");
    fireEvent.keyDown(sheet, { key: "Escape" });
    await act(() => nextRouter.settle());
    await waitFor(() => expect(screen.queryByTestId("approval-sheet")).toBeNull());
    expect(nextRouter.url).toBe(routes.approvals());
  });
});
