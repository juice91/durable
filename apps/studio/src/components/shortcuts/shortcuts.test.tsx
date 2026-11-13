import React, { useState } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { hydrateRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const nav = vi.hoisted(() => ({ push: vi.fn(), path: "/agents" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => ({}),
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

import { mockMatchMedia, type MatchMediaControl } from "@/components/frame/frameTesting";
import { DataList } from "@/components/shared/DataList";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/Dialog";
import { allNavItems } from "@/lib/nav";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations } from "@/test/a11y";
import { CREATE_ACTION_ATTRIBUTE, useCreateAction } from "./ShortcutsProvider";
import { KeyboardHarness, pressKey, pressKeys } from "./shortcutsTesting";

let media: MatchMediaControl;
beforeEach(() => {
  media = mockMatchMedia(true);
  nav.push.mockClear();
});
afterEach(() => {
  media.restore();
  vi.useRealTimers();
});

describe("g-key navigation", () => {
  test.each(allNavItems.filter((item) => item.gKey).map((item) => [item.gKey as string, item.href]))("g %s opens %s", (key, href) => {
    render(<KeyboardHarness />);
    pressKeys("g", key);
    expect(nav.push).toHaveBeenCalledWith(href);
  });

  test("g t opens Traces", () => {
    render(<KeyboardHarness />);
    pressKeys("g", "t");
    expect(nav.push).toHaveBeenCalledWith(routes.traces());
  });

  test("the letter must follow within 1 s", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    render(<KeyboardHarness />);
    pressKey("g");
    vi.advanceTimersByTime(1001);
    pressKey("a");
    expect(nav.push).not.toHaveBeenCalled();
  });

  test("typing g then a in an input does nothing", () => {
    render(<KeyboardHarness><input aria-label="Name" /></KeyboardHarness>);
    const input = screen.getByRole("textbox", { name: "Name" });
    input.focus();
    pressKey("g", {}, input);
    pressKey("a", {}, input);
    expect(nav.push).not.toHaveBeenCalled();
  });

  test("g typed in a field does not arm a sequence finished outside it", () => {
    render(<KeyboardHarness><input aria-label="Name" /></KeyboardHarness>);
    pressKey("g", {}, screen.getByRole("textbox", { name: "Name" }));
    pressKey("a");
    expect(nav.push).not.toHaveBeenCalled();
  });

  test("shortcuts are inert while a dialog is open", () => {
    render(
      <KeyboardHarness>
        <Dialog open>
          <DialogContent>
            <DialogTitle>Something</DialogTitle>
          </DialogContent>
        </Dialog>
      </KeyboardHarness>,
    );
    pressKeys("g", "a");
    pressKey("?");
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).toBeNull();
  });

  test("modifiers turn a key into something else", () => {
    render(<KeyboardHarness />);
    pressKey("g", { ctrlKey: true });
    pressKey("a", { ctrlKey: true });
    expect(nav.push).not.toHaveBeenCalled();
  });
});

describe("help dialog", () => {
  test("? opens it with every g-key; Escape closes it", async () => {
    render(<KeyboardHarness />);
    pressKey("?", { shiftKey: true });
    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    for (const item of allNavItems.filter((candidate) => candidate.gKey)) {
      expect(dialog.querySelector(`[data-shortcut-row="nav:${item.id}"]`)).toHaveTextContent(item.label);
    }
    expect(dialog.querySelector('[data-shortcut-row="palette"]')).toHaveTextContent("⌘");
    await expectNoAxeViolations(document.body);
    pressKey("Escape", {}, dialog);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).toBeNull());
  });
});

describe("c: the page's create action", () => {
  function WithAction({ onCreate }: { onCreate: () => void }) {
    useCreateAction(onCreate);
    return null;
  }

  test("runs the registered action; the latest registration wins and unmounting removes it", () => {
    const first = vi.fn();
    const second = vi.fn();
    function Page() {
      const [showSecond, setShowSecond] = useState(true);
      return (
        <>
          <WithAction onCreate={first} />
          {showSecond ? <WithAction onCreate={second} /> : null}
          <button type="button" onClick={() => setShowSecond(false)}>drop</button>
        </>
      );
    }
    render(<KeyboardHarness><Page /></KeyboardHarness>);
    pressKey("c");
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();
    act(() => screen.getByRole("button", { name: "drop" }).click());
    pressKey("c");
    expect(first).toHaveBeenCalledTimes(1);
  });

  test("without a registered action, clicks the page's declared create button", () => {
    const onClick = vi.fn();
    render(
      <KeyboardHarness>
        <button type="button" disabled {...{ [CREATE_ACTION_ATTRIBUTE]: "" }}>Disabled</button>
        <button type="button" onClick={onClick} {...{ [CREATE_ACTION_ATTRIBUTE]: "" }}>New agent</button>
      </KeyboardHarness>,
    );
    pressKey("c");
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  test("does nothing when the page has no create action, and nothing outside the provider", () => {
    const onCreate = vi.fn();
    render(<WithAction onCreate={onCreate} />);
    pressKey("c");
    expect(onCreate).not.toHaveBeenCalled();
  });
});

test("/ focuses the list search once (DataList binds it; the provider does not)", () => {
  const focus = vi.spyOn(HTMLInputElement.prototype, "focus");
  render(
    <KeyboardHarness>
      <DataList
        label="Agents"
        rows={[{ id: "a" }]}
        getRowId={(row: { id: string }) => row.id}
        columns={[{ id: "name", header: "Name", cell: (row: { id: string }) => row.id }]}
        searchPlaceholder="Search"
        searchLabel="Search agents"
        filterRow={() => true}
      />
    </KeyboardHarness>,
  );
  pressKey("/");
  expect(document.activeElement).toBe(screen.getByRole("searchbox", { name: "Search agents" }));
  expect(focus).toHaveBeenCalledTimes(1);
  focus.mockRestore();
});

test("the frame with the palette and shortcuts hydrates without mismatches; the search button appears after", async () => {
  const container = document.createElement("div");
  container.innerHTML = renderToString(<KeyboardHarness />);
  expect(container.querySelector('[data-testid="sidebar-search"]')).toBeNull();
  document.body.appendChild(container);
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  const recoverable = vi.fn();
  let root: Root | undefined;
  await act(async () => {
    root = hydrateRoot(container, <KeyboardHarness />, { onRecoverableError: recoverable });
  });
  expect(errors).not.toHaveBeenCalled();
  expect(recoverable).not.toHaveBeenCalled();
  expect(container.querySelector('[data-testid="sidebar-search"]')).not.toBeNull();
  errors.mockRestore();
  act(() => root?.unmount());
  container.remove();
});
