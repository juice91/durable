/** AGUI.13 acceptance: the Connect (AG-UI) Sheet: URL state, endpoints, secret-free snippets, copy, entry points. */
import React, { Suspense } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>{children}</a>
  ),
}));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/workflows")>()), listWorkflows: vi.fn(async () => []) }));

import { ConnectAgUiAction } from "./ConnectAgUiSheet";
import { WorkflowHeader } from "@/components/workflows/WorkflowHeader";
import { FrameHarness } from "@/components/frame/frameTesting";
import { BASE_URL } from "@/lib/api";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { nextRouter } from "@/test/nextRouterMock";

const writeText = vi.fn(async (_t: string) => undefined);
beforeEach(() => {
  writeText.mockClear();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Connect Sheet", () => {
  it("is URL-backed: the param opens it, Back closes it, a deep link opens it", async () => {
    nextRouter.reset(["/agents/ag-7/chat"]);
    const { unmount } = render(<Suspense><ConnectAgUiAction target={{ kind: "agent", id: "ag-7" }} name="Helper" /></Suspense>);
    expect(screen.queryByTestId("connect-agui-sheet")).toBeNull();
    fireEvent.click(screen.getByTestId("connect-agui-open"));
    await act(() => nextRouter.settle());
    expect(window.location.search).toBe("?connect=ag-ui");
    expect(await screen.findByTestId("connect-agui-sheet")).toBeInTheDocument();
    await act(async () => { nextRouter.back(); await nextRouter.settle(); });
    await waitFor(() => expect(screen.queryByTestId("connect-agui-sheet")).toBeNull());
    unmount();
    nextRouter.reset(["/agents/ag-7/chat?connect=ag-ui"]);
    render(<Suspense><ConnectAgUiAction target={{ kind: "agent", id: "ag-7" }} name="Helper" /></Suspense>);
    expect(await screen.findByTestId("connect-agui-sheet")).toBeInTheDocument();
  });

  it("an unknown ?connect= value does not open it", () => {
    nextRouter.reset(["/agents/a/chat?connect=other"]);
    render(<Suspense><ConnectAgUiAction target={{ kind: "agent", id: "a" }} name={null} /></Suspense>);
    expect(screen.queryByTestId("connect-agui-sheet")).toBeNull();
  });

  it.each([
    [{ kind: "agent", id: "ag-7" }, "agents"],
    [{ kind: "workflow", id: "wf-9" }, "workflows"],
  ] as const)("shows the exact endpoints for %j and no key anywhere", async (target, seg) => {
    nextRouter.reset([`/x?connect=ag-ui`]);
    render(<Suspense><ConnectAgUiAction target={target} name="N" /></Suspense>);
    const sheet = await screen.findByTestId("connect-agui-sheet");
    const value = (id: string) => (within(within(sheet).getByTestId(id)).getByRole("textbox") as HTMLInputElement).value;
    const root = `${BASE_URL.replace(/\/+$/, "")}/api/v1/ag-ui/${seg}/${target.id}`;
    expect(value("connect-agui-endpoint")).toBe(root);
    expect(value("connect-agui-thread")).toBe(`${root}/threads/{threadId}`);
    expect(value("connect-agui-attach")).toBe(`${root}/threads/{threadId}/runs/{runId}/stream`);
    for (const id of ["connect-agui-copilotkit", "connect-agui-curl"]) {
      const text = within(sheet).getByTestId(id).textContent ?? "";
      expect(text).toContain(root);
      expect(text).toContain("PYDANTICOS_API_KEY");
      expect(text).not.toMatch(/pos_[0-9a-f]/);
      expect(text).not.toMatch(/Bearer [A-Za-z0-9_-]{16,}/);
    }
    expect(sheet.innerHTML).not.toMatch(/pos_[0-9a-f]/);
    expect(within(sheet).getByTestId("connect-agui-keys-link")).toHaveAttribute("href", routes.settingsApiKeys());
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
    expect(window.localStorage.length).toBe(0);
  });

  it("copy buttons copy exactly the shown value, never a key, and report it politely", async () => {
    nextRouter.reset([`/x?connect=ag-ui`]);
    render(<Suspense><ConnectAgUiAction target={{ kind: "agent", id: "ag-7" }} name="N" /></Suspense>);
    const sheet = await screen.findByTestId("connect-agui-sheet");
    const field = within(sheet).getByTestId("connect-agui-endpoint");
    const shown = (within(field).getByRole("textbox") as HTMLInputElement).value;
    fireEvent.click(within(field).getByRole("button", { name: /Copy/ }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(shown));
    expect(await within(field).findByRole("status")).toHaveTextContent("Endpoint copied");
    const curl = within(sheet).getByTestId("connect-agui-curl");
    fireEvent.click(within(curl).getByRole("button", { name: /Copy/ }));
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith(within(curl).getByRole("code").textContent));
    expect(writeText.mock.calls.every(([t]) => !/pos_[0-9a-f]/.test(t))).toBe(true);
  });

  it("copy falls back to a message when the clipboard is unavailable", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    nextRouter.reset([`/x?connect=ag-ui`]);
    render(<Suspense><ConnectAgUiAction target={{ kind: "agent", id: "a" }} name="N" /></Suspense>);
    const field = within(await screen.findByTestId("connect-agui-sheet")).getByTestId("connect-agui-endpoint");
    fireEvent.click(within(field).getByRole("button", { name: /Copy/ }));
    expect(await within(field).findByRole("status")).toHaveTextContent(/not available/i);
  });

  it("axe and keyboard: the trigger is a button with a name and aria state; the Sheet is a dialog", async () => {
    nextRouter.reset([`/x?connect=ag-ui`]);
    render(<Suspense><ConnectAgUiAction target={{ kind: "workflow", id: "w" }} name="N" /></Suspense>);
    const sheet = await screen.findByTestId("connect-agui-sheet");
    const open = screen.getByTestId("connect-agui-open");
    expect(open.tagName).toBe("BUTTON");
    expect(open).toHaveAttribute("aria-expanded", "true");
    expect(open).toHaveAccessibleName("Connect (AG-UI)");
    expect(sheet).toHaveAttribute("role", "dialog");
    expect(sheet).toHaveAccessibleName(/Connect \(AG-UI\)/);
    await expectNoAxeViolations(sheet);
    // Snippets are keyboard-scrollable.
    for (const pre of sheet.querySelectorAll("pre")) expect(pre).toHaveAttribute("tabindex", "0");
  }, AXE_TIMEOUT_MS);

  it("375 px: the Sheet body has min-w-0 flex children and no fixed width over 375", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 375 });
    nextRouter.reset([`/x?connect=ag-ui`]);
    render(<Suspense><ConnectAgUiAction target={{ kind: "agent", id: "a".repeat(80) }} name="N" /></Suspense>);
    const body = await screen.findByTestId("connect-agui-body");
    expect(body.className).toMatch(/min-w-0/);
    const wide = Array.from(body.querySelectorAll<HTMLElement>("*")).filter((el) => Array.from(el.classList).some((c) => /^(min-)?w-\[(\d+)px\]$/.test(c) && Number(/\[(\d+)px\]/.exec(c)![1]) > 375));
    expect(wide).toEqual([]);
    for (const el of body.querySelectorAll("input")) expect(el.className).toMatch(/min-w-0/);
    for (const el of body.querySelectorAll("pre")) expect(el.className).toMatch(/overflow-auto/);
  });
});

describe("entry points", () => {
  it("the workflow header has the Connect button and opens the workflow endpoint", async () => {
    nextRouter.reset(["/workflows/wf-1"]);
    render(<Suspense><FrameHarness><WorkflowHeader workflowId="wf-1" name="Onboarding" tab="graph" /></FrameHarness></Suspense>);
    fireEvent.click(await screen.findByTestId("connect-agui-open"));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/workflows/wf-1?connect=ag-ui");
    const sheet = await screen.findByTestId("connect-agui-sheet");
    expect(within(sheet).getByDisplayValue(/\/api\/v1\/ag-ui\/workflows\/wf-1$/)).toBeInTheDocument();
    expect(within(sheet).getByText("Onboarding", { selector: "p, div, span" })).toBeInTheDocument();
  });
});
