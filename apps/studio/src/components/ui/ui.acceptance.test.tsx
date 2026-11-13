import React, { useState } from "react";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { expectNoAxeViolations } from "@/test/a11y";

const nav = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn(), back: vi.fn(), pathname: "/traces", search: "", fromWindow: false }));
// With `nav.fromWindow`, the path and query come from window.location and re-render on popstate,
// like the app router does on Back / Forward; otherwise from `nav.pathname` / `nav.search`.
vi.mock("next/navigation", async () => {
  const { useSyncExternalStore } = await import("react");
  const subscribe = (onChange: () => void) => {
    window.addEventListener("popstate", onChange);
    return () => window.removeEventListener("popstate", onChange);
  };
  return {
    useRouter: () => ({ replace: nav.replace, push: nav.push, back: nav.back }),
    usePathname: () => {
      const path = useSyncExternalStore(subscribe, () => window.location.pathname);
      return nav.fromWindow ? path : nav.pathname;
    },
    useSearchParams: () => {
      const search = useSyncExternalStore(subscribe, () => window.location.search);
      return new URLSearchParams(nav.fromWindow ? search : nav.search);
    },
  };
});

import { ConfirmDialog } from "../shared/ConfirmDialog";
import {
  Button, Combobox, Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
  Popover, PopoverContent, PopoverTrigger, ResizableGroup, ResizableHandle, ResizablePanel,
  SegmentedControl, Sheet, SheetContent, SheetHeader, SheetTrigger, Switch, Tooltip, TooltipProvider,
  openSheetIds, useUrlSheet,
} from "./index";

beforeEach(() => {
  nav.replace.mockReset();
  nav.push.mockReset();
  nav.back.mockReset();
  nav.pathname = "/traces";
  nav.search = "";
  nav.fromWindow = false;
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});

function Chain() {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const set = (k: string) => (v: boolean) => setOpen((o) => ({ ...o, [k]: v }));
  return (
    <>
      <button type="button" onClick={() => set("a")(true)}>open A</button>
      {(["a", "b", "c"] as const).map((k, i) => (
        <Sheet key={k} open={!!open[k]} onOpenChange={set(k)}>
          <SheetContent aria-describedby={undefined} data-testid={`sheet-${k}`}>
            <SheetHeader title={`Sheet ${k.toUpperCase()}`} />
            {i < 2 ? <button type="button" onClick={() => set(["b", "c"][i])(true)}>open {["B", "C"][i]}</button> : null}
          </SheetContent>
        </Sheet>
      ))}
    </>
  );
}

async function openChain() {
  render(<Chain />);
  const a = screen.getByRole("button", { name: "open A" });
  a.focus();
  fireEvent.click(a);
  const sa = await screen.findByRole("dialog", { name: "Sheet A" });
  const b = within(sa).getByRole("button", { name: "open B" });
  b.focus();
  fireEvent.click(b);
  const sb = await screen.findByRole("dialog", { name: "Sheet B" });
  return { a, b, sa, sb };
}

describe("Sheet acceptance", () => {
  it("three stacked: opening C closes the bottom A, B drops to depth 0, C sits at depth 1", async () => {
    const { sb } = await openChain();
    const c = within(sb).getByRole("button", { name: "open C" });
    c.focus();
    fireEvent.click(c);
    const sc = await screen.findByRole("dialog", { name: "Sheet C" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Sheet A" })).not.toBeInTheDocument());
    expect(openSheetIds()).toHaveLength(2);
    expect(screen.getByTestId("sheet-b")).toHaveAttribute("data-sheet-depth", "0");
    expect(sc).toHaveAttribute("data-sheet-depth", "1");
    expect(sc.style.right).toBe("24px");
    expect(screen.getByTestId("sheet-b").style.right).toBe("");
  });

  it("Escape closes only the top sheet and focus returns to each opener in turn", async () => {
    const { a, b } = await openChain();
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Sheet B" })).not.toBeInTheDocument());
    expect(screen.getByRole("dialog", { name: "Sheet A" })).toBeInTheDocument();
    await waitFor(() => expect(b).toHaveFocus());
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(a).toHaveFocus());
    expect(openSheetIds()).toEqual([]);
  });

  it("traps focus: Tab from the last control wraps to the first, Shift+Tab the reverse, never leaving the top sheet", async () => {
    const { sb } = await openChain();
    expect(sb.contains(document.activeElement)).toBe(true);
    const tabbables = Array.from(sb.querySelectorAll<HTMLElement>("button"));
    const first = tabbables[0];
    const last = tabbables[tabbables.length - 1];
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    // Focus that escapes (programmatically) is pulled back in.
    act(() => Array.from(document.querySelectorAll("button")).find((el) => el.textContent === "open A")!.focus());
    await waitFor(() => expect(sb.contains(document.activeElement)).toBe(true));
  });

  it("sizes: md 480px, wide 60vw, full; every size is w-full below md", () => {
    const sizes = { md: /md:w-\[480px\]/, wide: /md:w-\[60vw\]/, full: null } as const;
    for (const [size, md] of Object.entries(sizes)) {
      const { unmount } = render(
        <Sheet defaultOpen>
          <SheetContent size={size as "md" | "wide" | "full"} aria-describedby={undefined}><SheetHeader title={`S ${size}`} /></SheetContent>
        </Sheet>,
      );
      const dialog = screen.getByRole("dialog", { name: `S ${size}` });
      expect(dialog).toHaveAttribute("data-sheet-size", size);
      expect(dialog.className).toMatch(/(^|\s)w-full(\s|$)/);
      if (md) expect(dialog.className).toMatch(md);
      else expect(dialog.className).not.toMatch(/md:w-/);
      unmount();
    }
  });

  it("has no axe violations with a stack of two sheets open", async () => {
    await openChain();
    await expectNoAxeViolations(document.body);
  });
});

function Url({ history, clear }: { history?: "push" | "replace"; clear?: string[] }) {
  const sheet = useUrlSheet("trace", { history, clear });
  return (
    <>
      <button type="button" onClick={() => sheet.open("r1", { tab: "logs", x: null })}>go</button>
      <Sheet open={sheet.isOpen} onOpenChange={sheet.onOpenChange}>
        <SheetContent aria-describedby={undefined}><SheetHeader title={`T ${sheet.value}`} /></SheetContent>
      </Sheet>
    </>
  );
}

describe("useUrlSheet acceptance", () => {
  it("replace is the default for open and close; push mode pushes the open and closes with Back", () => {
    const { unmount } = render(<Url />);
    fireEvent.click(screen.getByRole("button", { name: "go" }));
    expect(nav.replace).toHaveBeenCalledTimes(1);
    expect(nav.push).not.toHaveBeenCalled();
    unmount();
    nav.replace.mockReset();
    nav.search = "trace=r1";
    render(<Url history="push" />);
    fireEvent.click(screen.getByRole("button", { name: "go", hidden: true }));
    expect(nav.push).toHaveBeenCalledWith("/traces?trace=r1&tab=logs", { scroll: false });
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    // Decided change (SL.2 review): closing a Sheet this hook pushed goes Back instead of pushing
    // the closed URL, so the browser's Back button never reopens it.
    expect(nav.back).toHaveBeenCalledTimes(1);
    expect(nav.push).toHaveBeenCalledTimes(1);
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("close clears the extra keys and keeps unrelated ones", () => {
    nav.search = "status=failed&trace=r1&tab=logs&page=2";
    render(<Url clear={["tab", "page"]} />);
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    expect(nav.replace).toHaveBeenLastCalledWith("/traces?status=failed", { scroll: false });
  });

  it("deep-linking opens on mount and a value is exposed; no param means closed", async () => {
    nav.search = "trace=r9";
    const { unmount } = render(<Url />);
    expect(await screen.findByRole("dialog", { name: "T r9" })).toBeInTheDocument();
    unmount();
    nav.search = "";
    render(<Url />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("follows the URL on back/forward (popstate): the sheet closes and reopens as the param changes", async () => {
    nav.fromWindow = true;
    window.history.pushState(null, "", "/traces?trace=r1");
    render(<Url history="push" />);
    expect(await screen.findByRole("dialog", { name: "T r1" })).toBeInTheDocument();
    // Back: the browser restores the earlier URL, then fires popstate.
    act(() => {
      window.history.pushState(null, "", "/traces");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // Forward to a different trace.
    act(() => {
      window.history.pushState(null, "", "/traces?trace=r2");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByRole("dialog", { name: "T r2" })).toBeInTheDocument();
    // Following the URL must not itself navigate.
    expect(nav.push).not.toHaveBeenCalled();
    expect(nav.replace).not.toHaveBeenCalled();
    expect(nav.back).not.toHaveBeenCalled();
  });
});

function ConfirmHarness({ busy = false, error = null as string | null, onConfirm = () => {} }) {
  const [show, setShow] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setShow(true)}>ask</button>
      <p>page content</p>
      {show ? <ConfirmDialog title="Delete run?" description="This cannot be undone." confirmLabel="Delete" destructive busy={busy} error={error} onConfirm={onConfirm} onCancel={() => setShow(false)} /> : null}
    </>
  );
}

describe("ConfirmDialog acceptance", () => {
  it("focuses Cancel, makes the page inert, Escape cancels and focus returns to the opener", async () => {
    const { container } = render(<ConfirmHarness />);
    const ask = screen.getByRole("button", { name: "ask" });
    ask.focus();
    fireEvent.click(ask);
    const dialog = await screen.findByRole("dialog", { name: "Delete run?" });
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    expect(dialog).toHaveAccessibleDescription("This cannot be undone.");
    expect(container).toHaveAttribute("inert");
    await expectNoAxeViolations(document.body);
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(ask).toHaveFocus();
    expect(container).not.toHaveAttribute("inert");
  });

  it("busy blocks Escape and disables both buttons without losing focus to the body", async () => {
    const { rerender } = render(<ConfirmHarness />);
    fireEvent.click(screen.getByRole("button", { name: "ask" }));
    await screen.findByRole("dialog");
    rerender(<ConfirmHarness busy />);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    rerender(<ConfirmHarness />);
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("shows the error as an alert inside the dialog, calls onConfirm, ignores outside clicks; axe clean", async () => {
    const onConfirm = vi.fn();
    render(<ConfirmHarness error="Server said no" onConfirm={onConfirm} />);
    fireEvent.click(screen.getByRole("button", { name: "ask" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Server said no");
    expect(screen.getByTestId("confirm-dialog-error")).toBeInTheDocument();
    fireEvent.pointerDown(document.body);
    fireEvent.click(document.body);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await expectNoAxeViolations(document.body);
  });
});

describe("Popover, DropdownMenu, Tooltip", () => {
  it("Popover: trigger exposes aria-expanded/haspopup, Escape closes and returns focus, axe clean", async () => {
    render(
      <Popover>
        <PopoverTrigger asChild><Button>Filters</Button></PopoverTrigger>
        <PopoverContent aria-label="Filter options"><button type="button">Apply</button></PopoverContent>
      </Popover>,
    );
    const trigger = screen.getByRole("button", { name: "Filters" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    trigger.focus();
    fireEvent.click(trigger);
    expect(await screen.findByRole("dialog", { name: "Filter options" })).toBeInTheDocument();
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    await expectNoAxeViolations(document.body);
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("DropdownMenu: Enter opens, arrows move over menuitems, Enter selects, Escape closes without selecting", async () => {
    const onA = vi.fn();
    const onB = vi.fn();
    render(
      <DropdownMenu>
        <DropdownMenuTrigger asChild><Button>Actions</Button></DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem onSelect={onA}>Copy id</DropdownMenuItem>
          <DropdownMenuItem onSelect={onB}>Delete</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>,
    );
    const trigger = screen.getByRole("button", { name: "Actions" });
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem")).toHaveLength(2);
    await expectNoAxeViolations(document.body);
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowDown" });
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(onA).not.toHaveBeenCalled();
    await waitFor(() => expect(trigger).toHaveFocus());
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await screen.findByRole("menu");
    await waitFor(() => expect(screen.getByRole("menuitem", { name: "Copy id" })).toHaveFocus());
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowDown" });
    await waitFor(() => expect(screen.getByRole("menuitem", { name: "Delete" })).toHaveFocus());
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Enter" });
    expect(onB).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
  });

  it("Tooltip: wires aria-describedby on focus, closes on Escape, axe clean", async () => {
    render(<Tooltip content="Copy the run id"><button type="button">Copy</button></Tooltip>, { wrapper: TooltipProvider });
    const button = screen.getByRole("button", { name: "Copy" });
    act(() => button.focus());
    const tip = await screen.findByRole("tooltip");
    expect(tip).toHaveTextContent("Copy the run id");
    expect(button.getAttribute("aria-describedby")).toBeTruthy();
    await expectNoAxeViolations(document.body);
    fireEvent.keyDown(button, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
  });
});

describe("Combobox and Command", () => {
  const options = [
    { value: "a1", label: "Support bot" },
    { value: "a2", label: "Billing helper" },
    { value: "a3", label: "Archivist", disabled: true },
  ];

  it("Combobox: keyboard opens, search focused, ArrowDown+Enter selects, Escape returns focus", async () => {
    const onValueChange = vi.fn();
    render(<Combobox label="Switch agent" value="a1" onValueChange={onValueChange} options={options} />);
    const trigger = screen.getByRole("combobox", { name: "Switch agent" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    trigger.focus();
    fireEvent.click(trigger);
    const search = await screen.findByPlaceholderText("Search…");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    await waitFor(() => expect(search).toHaveFocus());
    const listbox = screen.getByRole("listbox");
    expect(within(listbox).getAllByRole("option")).toHaveLength(3);
    fireEvent.keyDown(search, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(onValueChange).not.toHaveBeenCalled();

    fireEvent.click(trigger);
    const again = await screen.findByPlaceholderText("Search…");
    await waitFor(() => expect(screen.getByRole("option", { name: "Support bot" })).toHaveAttribute("aria-selected", "true"));
    fireEvent.keyDown(again, { key: "ArrowDown" });
    await waitFor(() => expect(screen.getByRole("option", { name: "Billing helper" })).toHaveAttribute("aria-selected", "true"));
    expect(screen.getByRole("option", { name: "Support bot" })).toHaveAttribute("aria-selected", "false");
    fireEvent.keyDown(again, { key: "Enter" });
    expect(onValueChange).toHaveBeenCalledWith("a2");
    expect(onValueChange).toHaveBeenCalledTimes(1);
  });

  it("Combobox: the open popover is axe clean (the dialog needs an accessible name)", async () => {
    render(<Combobox label="Switch agent" value="a1" onValueChange={() => {}} options={options} />);
    fireEvent.click(screen.getByRole("combobox", { name: "Switch agent" }));
    await screen.findByRole("listbox");
    await expectNoAxeViolations(document.body);
  });

  it("Combobox: shows the empty text when nothing matches and marks the disabled option", async () => {
    render(<Combobox label="Pick" value={null} onValueChange={() => {}} options={options} emptyText="Nothing found" />);
    fireEvent.click(screen.getByRole("combobox", { name: "Pick" }));
    const search = await screen.findByPlaceholderText("Search…");
    expect(screen.getByRole("option", { name: /Archivist/ })).toHaveAttribute("aria-disabled", "true");
    fireEvent.change(search, { target: { value: "zzz" } });
    expect(await screen.findByText("Nothing found")).toBeInTheDocument();
  });

  it("Command: input is a combobox over a listbox; arrows move the selection and Enter runs the item; axe clean", async () => {
    const onRun = vi.fn();
    render(
      <Command label="Palette">
        <CommandInput placeholder="Type a command" />
        <CommandList>
          <CommandEmpty>No results</CommandEmpty>
          <CommandGroup heading="Pages">
            <CommandItem value="agents" onSelect={onRun}>Agents</CommandItem>
            <CommandItem value="traces" onSelect={onRun}>Traces</CommandItem>
          </CommandGroup>
        </CommandList>
      </Command>,
    );
    const input = screen.getByRole("combobox");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.getByRole("option", { name: "Agents" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(screen.getByRole("option", { name: "Traces" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRun).toHaveBeenCalledWith("traces");
    await expectNoAxeViolations(document.body);
  });

  it("Command: the no-results state is axe clean too", async () => {
    render(
      <Command label="Palette">
        <CommandInput placeholder="Type a command" />
        <CommandList>
          <CommandEmpty>No results</CommandEmpty>
          <CommandGroup heading="Pages"><CommandItem value="agents">Agents</CommandItem></CommandGroup>
        </CommandList>
      </Command>,
    );
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "qqq" } });
    expect(await screen.findByText("No results")).toBeInTheDocument();
    await expectNoAxeViolations(document.body);
  });
});

function Panes({ storageKey }: { storageKey?: string }) {
  return (
    <div style={{ width: 1000, height: 400 }}>
      <ResizableGroup storageKey={storageKey} defaultLayout={{ left: 50, right: 50 }}>
        <ResizablePanel id="left">L</ResizablePanel>
        <ResizableHandle label="Resize left panel" />
        <ResizablePanel id="right">R</ResizablePanel>
      </ResizableGroup>
    </div>
  );
}
const flexGrow = (id: string) => document.querySelector<HTMLElement>(`[data-panel][id='${id}']`)?.style.flexGrow;

describe("Resizable acceptance", () => {
  // jsdom does no layout; give every element a 1000 x 400 box so the panel group can resize.
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(1000);
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(400);
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const id = this.getAttribute("id");
      const w = this.getAttribute("role") === "separator" ? 1 : id === "left" ? 500 : id === "right" ? 500 : 1000;
      const x = id === "right" ? 500 : 0;
      return { x, y: 0, left: x, top: 0, width: w, height: 400, right: x + w, bottom: 400, toJSON: () => ({}) } as DOMRect;
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("saves nothing on mount, saves after a keyboard resize, and restores per storageKey", async () => {
    const { unmount } = render(<Panes storageKey="k.one" />);
    await new Promise((r) => setTimeout(r, 50));
    expect(window.localStorage.getItem("k.one")).toBeNull();
    const handle = screen.getByRole("separator", { name: "Resize left panel" });
    expect(handle).toHaveAttribute("tabindex", "0");
    handle.focus();
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    await waitFor(() => expect(window.localStorage.getItem("k.one")).not.toBeNull());
    const saved = JSON.parse(window.localStorage.getItem("k.one") as string) as Record<string, number>;
    expect(saved.left).toBeGreaterThan(50);
    expect(saved.left + saved.right).toBeCloseTo(100, 0);
    expect(window.localStorage.getItem("k.two")).toBeNull();
    unmount();

    render(<Panes storageKey="k.one" />);
    expect(Number(flexGrow("left"))).toBeCloseTo(saved.left, 1);
    document.body.innerHTML = "";
    render(<Panes storageKey="k.two" />);
    expect(Number(flexGrow("left"))).toBeCloseTo(50, 1);
  });

  it("ignores bad stored data and uses the default layout; Home/End resize the separator", async () => {
    window.localStorage.setItem("k.bad", '{"left": 999, "right": "x"}');
    render(<Panes storageKey="k.bad" />);
    expect(Number(flexGrow("left"))).toBeCloseTo(50, 1);
    const handle = screen.getByRole("separator", { name: "Resize left panel" });
    expect(handle).toHaveAttribute("aria-valuenow");
    handle.focus();
    fireEvent.keyDown(handle, { key: "Home" });
    await waitFor(() => expect(Number(handle.getAttribute("aria-valuenow"))).toBeLessThan(50));
    fireEvent.keyDown(handle, { key: "End" });
    await waitFor(() => expect(Number(handle.getAttribute("aria-valuenow"))).toBeGreaterThan(50));
    await expectNoAxeViolations(document.body);
  });
});

describe("SegmentedControl and Switch acceptance", () => {
  function Seg() {
    const [v, setV] = useState("system");
    return <SegmentedControl label="Theme" value={v} onValueChange={setV} options={[{ value: "system", label: "System" }, { value: "light", label: "Light" }, { value: "dark", label: "Dark" }]} />;
  }

  it("SegmentedControl: radiogroup with labelled radios, arrows move focus, Space/Enter select, clicking the active one keeps it", async () => {
    render(<Seg />);
    const group = screen.getByRole("radiogroup", { name: "Theme" });
    const radios = within(group).getAllByRole("radio");
    expect(radios).toHaveLength(3);
    expect(radios[0]).toHaveAttribute("aria-checked", "true");
    radios[0].focus();
    fireEvent.keyDown(radios[0], { key: "ArrowRight" });
    await waitFor(() => expect(radios[1]).toHaveFocus());
    fireEvent.keyDown(radios[1], { key: "Enter" });
    expect(radios[1]).toHaveAttribute("aria-checked", "true");
    expect(radios[0]).toHaveAttribute("aria-checked", "false");
    fireEvent.keyDown(radios[1], { key: "ArrowRight" });
    await waitFor(() => expect(radios[2]).toHaveFocus());
    fireEvent.keyDown(radios[2], { key: " " });
    expect(radios[2]).toHaveAttribute("aria-checked", "true");
    expect(radios[1]).toHaveAttribute("aria-checked", "false");
    fireEvent.click(radios[2]);
    expect(radios[2]).toHaveAttribute("aria-checked", "true");
    await expectNoAxeViolations(document.body);
  });

  it("Switch: role=switch, aria-checked toggles with Space/click, disabled does not toggle; axe clean", async () => {
    const onChange = vi.fn();
    render(<><Switch aria-label="Auto refresh" onCheckedChange={onChange} /><Switch aria-label="Locked" disabled /></>);
    const sw = screen.getByRole("switch", { name: "Auto refresh" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    sw.focus();
    fireEvent.click(sw);
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(onChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(sw);
    expect(onChange).toHaveBeenLastCalledWith(false);
    const locked = screen.getByRole("switch", { name: "Locked" });
    expect(locked).toBeDisabled();
    fireEvent.click(locked);
    expect(locked).toHaveAttribute("aria-checked", "false");
    await expectNoAxeViolations(document.body);
  });
});

describe("kit hygiene", () => {
  const files = readdirSync(__dirname).filter((f) => /\.(tsx?|css)$/.test(f) && !/\.test\./.test(f));
  const read = (f: string) => readFileSync(join(__dirname, f), "utf8");

  it("has no hardcoded colours in components/ui", () => {
    const offenders: string[] = [];
    for (const f of files) {
      const src = read(f);
      if (/#[0-9a-fA-F]{3,8}\b/.test(src)) offenders.push(`${f}: hex`);
      if (/\b(rgba?|hsla?|oklch|oklab|lab|lch)\(/.test(src)) offenders.push(`${f}: colour function`);
      if (/\b(bg|text|border|ring|fill|stroke|from|to|via|outline|divide|shadow)-(\w+-)?(slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|white|black)(-\d{2,3})?\b/.test(src)) offenders.push(`${f}: palette utility`);
      if (/\[#|\[rgb|\[hsl/.test(src)) offenders.push(`${f}: arbitrary colour`);
    }
    expect(offenders).toEqual([]);
  });

  it("has no Mastra branding strings in components/ui", () => {
    const offenders = files.filter((f) => /mastra/i.test(read(f)));
    expect(offenders).toEqual([]);
    expect(files.length).toBeGreaterThan(15);
  });
});
