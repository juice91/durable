import React, { useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { expectNoAxeViolations } from "@/test/a11y";
import { cn } from "@/lib/cn";
import { AgentsIcon, GitGraphIcon, ICON_STROKE_WIDTH } from "@/lib/icons";

const navigation = vi.hoisted(() => ({
  replace: vi.fn(),
  push: vi.fn(),
  back: vi.fn(),
  pathname: "/traces",
  search: "",
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: navigation.replace, push: navigation.push, back: navigation.back }),
  usePathname: () => navigation.pathname,
  useSearchParams: () => new URLSearchParams(navigation.search),
}));

import {
  Button,
  Combobox,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  IconButton,
  LinkTabs,
  ResizableGroup,
  ResizableHandle,
  ResizablePanel,
  SegmentedControl,
  Sheet,
  Tooltip,
  TooltipProvider,
  SheetBody,
  SheetContent,
  SheetHeader,
  Switch,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  isLinkTabActive,
  openSheetIds,
  readStoredLayout,
  useUrlSheet,
  withSearchParams,
  writeStoredLayout,
} from "./index";

beforeEach(() => {
  navigation.replace.mockReset();
  navigation.push.mockReset();
  navigation.back.mockReset();
  window.history.replaceState(null, "", "/");
  navigation.pathname = "/traces";
  navigation.search = "";
  window.localStorage.clear();
});

describe("cn", () => {
  it("keeps Studio's custom text sizes next to text colours and merges conflicting scales", () => {
    expect(cn("text-label text-foreground", "text-muted-foreground")).toBe("text-label text-muted-foreground");
    expect(cn("text-label", "text-caption")).toBe("text-caption");
    expect(cn("h-row", "h-8")).toBe("h-8");
    expect(cn("shadow-raised", "shadow-overlay")).toBe("shadow-overlay");
  });
});

describe("icons", () => {
  it("renders lucide SVGs with Studio's stroke and keeps the old names", () => {
    const { container } = render(
      <>
        <AgentsIcon className="size-4" />
        <GitGraphIcon className="size-4" />
      </>,
    );
    const svgs = container.querySelectorAll("svg");
    expect(svgs).toHaveLength(2);
    svgs.forEach((svg) => {
      expect(svg).toHaveAttribute("stroke-width", String(ICON_STROKE_WIDTH));
      expect(svg).toHaveAttribute("viewBox", "0 0 24 24");
      expect(svg).toHaveAttribute("aria-hidden", "true");
    });
  });
});

describe("Button and IconButton", () => {
  it("defaults to type=button and gives icon buttons an accessible name", async () => {
    render(
      <>
        <Button>Save</Button>
        <IconButton label="Copy run id" icon={<AgentsIcon />} />
      </>,
      { wrapper: TooltipProvider },
    );
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("type", "button");
    expect(screen.getByRole("button", { name: "Copy run id" })).toBeInTheDocument();
    await expectNoAxeViolations(document.body);
  });
});

function DialogHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Open dialog</button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogTitle>Rename</DialogTitle>
          <DialogDescription>Pick a new name.</DialogDescription>
          <input aria-label="Name" />
        </DialogContent>
      </Dialog>
    </>
  );
}

describe("Dialog", () => {
  it("opens without a Trigger, closes on Escape and returns focus to the opener", async () => {
    render(<DialogHarness />);
    const opener = screen.getByRole("button", { name: "Open dialog" });
    opener.focus();
    fireEvent.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Rename" });
    await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement));
    await expectNoAxeViolations(document.body);

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(opener).toHaveFocus());
  });
});

function SheetPair({ thirdOpen = false }: { thirdOpen?: boolean }) {
  const [first, setFirst] = useState(false);
  const [second, setSecond] = useState(false);
  const [third, setThird] = useState(thirdOpen);
  return (
    <>
      <button type="button" onClick={() => setFirst(true)}>Open log</button>
      <Sheet open={first} onOpenChange={setFirst}>
        <SheetContent aria-describedby={undefined}>
          <SheetHeader title="Log line" />
          <SheetBody>
            <button type="button" onClick={() => setSecond(true)}>Open trace</button>
          </SheetBody>
        </SheetContent>
      </Sheet>
      <Sheet open={second} onOpenChange={setSecond}>
        <SheetContent size="wide" aria-describedby={undefined}>
          <SheetHeader title="Trace" />
          <SheetBody>
            <button type="button" onClick={() => setThird(true)}>Open child run</button>
          </SheetBody>
        </SheetContent>
      </Sheet>
      <Sheet open={third} onOpenChange={setThird}>
        <SheetContent aria-describedby={undefined}>
          <SheetHeader title="Child run" />
        </SheetContent>
      </Sheet>
    </>
  );
}

describe("Sheet", () => {
  it("stacks a second Sheet 24 px in, closes only the top one on Escape, and returns focus each time", async () => {
    render(<SheetPair />);
    const openLog = screen.getByRole("button", { name: "Open log" });
    openLog.focus();
    fireEvent.click(openLog);
    const log = await screen.findByRole("dialog", { name: "Log line" });
    expect(log).toHaveAttribute("data-sheet-depth", "0");
    expect(log).toHaveAttribute("data-sheet-size", "md");
    await expectNoAxeViolations(document.body);

    const openTrace = within(log).getByRole("button", { name: "Open trace" });
    openTrace.focus();
    fireEvent.click(openTrace);
    const trace = await screen.findByRole("dialog", { name: "Trace" });
    expect(trace).toHaveAttribute("data-sheet-depth", "1");
    expect(trace.style.right).toBe("24px");
    expect(openSheetIds()).toHaveLength(2);

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Trace" })).not.toBeInTheDocument());
    expect(screen.getByRole("dialog", { name: "Log line" })).toBeInTheDocument();
    await waitFor(() => expect(openTrace).toHaveFocus());

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(openLog).toHaveFocus());
    expect(openSheetIds()).toHaveLength(0);
  });

  it("hands the auto-closed bottom Sheet's focus target up: closing C then B ends on A's opener", async () => {
    render(<SheetPair />);
    const openLog = screen.getByRole("button", { name: "Open log" });
    openLog.focus();
    fireEvent.click(openLog);
    const openTrace = within(await screen.findByRole("dialog", { name: "Log line" })).getByRole("button", { name: "Open trace" });
    openTrace.focus();
    fireEvent.click(openTrace);
    const openChild = within(await screen.findByRole("dialog", { name: "Trace" })).getByRole("button", { name: "Open child run" });
    openChild.focus();
    fireEvent.click(openChild);
    const child = await screen.findByRole("dialog", { name: "Child run" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Log line" })).not.toBeInTheDocument());
    // The auto-closed Sheet returns no focus of its own: focus stays in the new top Sheet.
    await waitFor(() => expect(child).toContainElement(document.activeElement as HTMLElement));

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Child run" })).not.toBeInTheDocument());
    await waitFor(() => expect(openChild).toHaveFocus());
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(openLog).toHaveFocus());
  });

  it("closes the bottom Sheet when a third one opens", async () => {
    render(<SheetPair />);
    fireEvent.click(screen.getByRole("button", { name: "Open log" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Log line" })).getByRole("button", { name: "Open trace" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Trace" })).getByRole("button", { name: "Open child run" }));
    await screen.findByRole("dialog", { name: "Child run" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Log line" })).not.toBeInTheDocument());
    expect(openSheetIds()).toHaveLength(2);
    expect(screen.getByRole("dialog", { name: "Child run" })).toHaveAttribute("data-sheet-depth", "1");
  });
});

function UrlSheetHarness() {
  const sheet = useUrlSheet("trace", { clear: ["tab"] });
  return (
    <>
      <button type="button" onClick={() => sheet.open("run:1", { tab: "events" })}>Open run</button>
      <Sheet open={sheet.isOpen} onOpenChange={sheet.onOpenChange}>
        <SheetContent aria-describedby={undefined}>
          <SheetHeader title={`Trace ${sheet.value ?? ""}`} />
        </SheetContent>
      </Sheet>
    </>
  );
}

describe("useUrlSheet", () => {
  it("builds query strings with additions and removals", () => {
    expect(withSearchParams("/traces", new URLSearchParams("status=failed"), { trace: "a b", tab: null })).toBe("/traces?status=failed&trace=a+b");
    expect(withSearchParams("/traces", new URLSearchParams("trace=1"), { trace: null })).toBe("/traces");
  });

  it("opens from the URL parameter and removes it (and the cleared ones) on close", async () => {
    navigation.search = "status=failed";
    const { rerender } = render(<UrlSheetHarness />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open run" }));
    expect(navigation.replace).toHaveBeenCalledWith("/traces?status=failed&tab=events&trace=run%3A1", { scroll: false });

    navigation.search = "status=failed&tab=events&trace=run%3A1";
    rerender(<UrlSheetHarness />);
    expect(await screen.findByRole("dialog", { name: "Trace run:1" })).toBeInTheDocument();
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    expect(navigation.replace).toHaveBeenLastCalledWith("/traces?status=failed", { scroll: false });
  });
});

function PushSheetHarness() {
  const sheet = useUrlSheet("tool", { history: "push" });
  return (
    <>
      <button type="button" onClick={() => sheet.open("search")}>Open tool</button>
      <button type="button" onClick={() => sheet.open("fetch")}>Switch tool</button>
      <span data-testid="tool-state">{sheet.isOpen ? `open:${sheet.value}` : "closed"}</span>
      <button type="button" onClick={sheet.close}>Close tool</button>
    </>
  );
}

describe("useUrlSheet history and URL source", () => {
  it("push mode: the first open pushes, a switch replaces, and closing goes Back", () => {
    navigation.pathname = "/tools";
    const { rerender } = render(<PushSheetHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Open tool" }));
    expect(navigation.push).toHaveBeenCalledWith("/tools?tool=search", { scroll: false });
    navigation.search = "tool=search";
    rerender(<PushSheetHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Switch tool" }));
    expect(navigation.push).toHaveBeenCalledTimes(1);
    expect(navigation.replace).toHaveBeenCalledWith("/tools?tool=fetch", { scroll: false });
    fireEvent.click(screen.getByRole("button", { name: "Close tool" }));
    expect(navigation.back).toHaveBeenCalledTimes(1);
    expect(navigation.replace).toHaveBeenCalledTimes(1);
  });

  it("push mode: closing after a link changed the value replaces instead of going Back", () => {
    navigation.pathname = "/tools";
    const { rerender } = render(<PushSheetHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Open tool" }));
    navigation.search = "tool=search";
    rerender(<PushSheetHarness />);
    // A link elsewhere moves the URL to a value this hook never wrote.
    navigation.search = "tool=other";
    rerender(<PushSheetHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Close tool" }));
    expect(navigation.back).not.toHaveBeenCalled();
    expect(navigation.replace).toHaveBeenCalledWith("/tools", { scroll: false });
  });

  it("push mode: closing a deep-linked Sheet replaces (there is nothing of ours to go Back to)", () => {
    navigation.pathname = "/tools";
    navigation.search = "tool=search";
    render(<PushSheetHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Close tool" }));
    expect(navigation.back).not.toHaveBeenCalled();
    expect(navigation.replace).toHaveBeenCalledWith("/tools", { scroll: false });
  });

  it("treats an empty parameter as closed", () => {
    navigation.pathname = "/tools";
    navigation.search = "tool=";
    render(<PushSheetHarness />);
    expect(screen.getByTestId("tool-state")).toHaveTextContent("closed");
  });

  it("builds the next URL from the live location when the window is on the same path", () => {
    navigation.pathname = "/tools";
    navigation.search = "q=old";
    window.history.replaceState(null, "", "/tools?q=new&page=2");
    render(<PushSheetHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Open tool" }));
    expect(navigation.push).toHaveBeenCalledWith("/tools?q=new&page=2&tool=search", { scroll: false });
  });
});

describe("Tabs and LinkTabs", () => {
  it("switches Radix tabs with the keyboard", () => {
    render(
      <Tabs defaultValue="overview">
        <TabsList aria-label="Tool">
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="run">Run</TabsTrigger>
        </TabsList>
        <TabsContent value="overview">Schemas</TabsContent>
        <TabsContent value="run">Runner</TabsContent>
      </Tabs>,
    );
    const overview = screen.getByRole("tab", { name: "Overview" });
    expect(overview).toHaveAttribute("aria-selected", "true");
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Run" }), { button: 0 });
    expect(screen.getByRole("tab", { name: "Run" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Runner");
  });

  it("marks the route tab for the current path and its sub-paths", async () => {
    navigation.pathname = "/agents/a1/chat/c1";
    render(
      <LinkTabs
        label="Agent sections"
        items={[
          { href: "/agents/a1/chat", label: "Chat" },
          { href: "/agents/a1/traces", label: "Traces" },
          { href: "/agents/a1/editor?step=tools", label: "Editor" },
        ]}
      />,
    );
    const nav = screen.getByRole("navigation", { name: "Agent sections" });
    expect(within(nav).getByRole("link", { name: "Chat" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Traces" })).not.toHaveAttribute("aria-current");
    expect(isLinkTabActive("/agents/a1/editor?step=tools", "/agents/a1/editor")).toBe(true);
    expect(isLinkTabActive("/agents/a1", "/agents/a10")).toBe(false);
    expect(isLinkTabActive("/", "/agents")).toBe(false);
    expect(isLinkTabActive("/", "/")).toBe(true);
    await expectNoAxeViolations(document.body);
  });
});

describe("SegmentedControl and Switch", () => {
  it("keeps one option selected and moves with the keyboard", async () => {
    const onValueChange = vi.fn();
    render(
      <SegmentedControl
        label="Theme"
        value="system"
        onValueChange={onValueChange}
        options={[
          { value: "system", label: "System" },
          { value: "light", label: "Light" },
          { value: "dark", label: "Dark" },
        ]}
      />,
    );
    const group = screen.getByRole("radiogroup", { name: "Theme" });
    const system = within(group).getByRole("radio", { name: "System" });
    expect(system).toHaveAttribute("aria-checked", "true");
    act(() => system.focus());
    fireEvent.keyDown(system, { key: "ArrowRight" });
    await waitFor(() => expect(within(group).getByRole("radio", { name: "Light" })).toHaveFocus());
    fireEvent.click(system);
    expect(onValueChange).not.toHaveBeenCalled();
    fireEvent.click(within(group).getByRole("radio", { name: "Dark" }));
    expect(onValueChange).toHaveBeenCalledWith("dark");
    await expectNoAxeViolations(document.body);
  });

  it("toggles a labelled switch", () => {
    const onCheckedChange = vi.fn();
    render(<Switch aria-label="Wrap instructions" onCheckedChange={onCheckedChange} />);
    fireEvent.click(screen.getByRole("switch", { name: "Wrap instructions" }));
    expect(onCheckedChange).toHaveBeenCalledWith(true);
  });
});

describe("DropdownMenu", () => {
  it("opens from the keyboard, runs an item and returns focus to the trigger", async () => {
    const onSelect = vi.fn();
    render(
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button>Version actions</Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem onSelect={onSelect} shortcut="R">Restore this version</DropdownMenuItem>
          <DropdownMenuItem destructive>Delete</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>,
    );
    const trigger = screen.getByRole("button", { name: "Version actions" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    const menu = await screen.findByRole("menu");
    await expectNoAxeViolations(document.body);
    fireEvent.click(within(menu).getByRole("menuitem", { name: /Restore this version/ }));
    expect(onSelect).toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});

describe("Combobox", () => {
  it("filters and selects an option, then closes", async () => {
    const onValueChange = vi.fn();
    render(
      <Combobox
        label="Switch agent"
        value="a1"
        onValueChange={onValueChange}
        options={[
          { value: "a1", label: "Support bot" },
          { value: "a2", label: "Billing helper", description: "Answers invoice questions" },
        ]}
      />,
    );
    const trigger = screen.getByRole("combobox", { name: "Switch agent" });
    expect(trigger).toHaveTextContent("Support bot");
    fireEvent.click(trigger);
    const search = await screen.findByPlaceholderText("Search…");
    fireEvent.change(search, { target: { value: "billing" } });
    await waitFor(() => expect(screen.queryByRole("option", { name: /Support bot/ })).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("option", { name: /Billing helper/ }));
    expect(onValueChange).toHaveBeenCalledWith("a2");
    await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());
  });
});

describe("Resizable", () => {
  it("reads only well-formed stored layouts and survives storage errors", () => {
    writeStoredLayout("k", { threads: 25, chat: 75 });
    expect(readStoredLayout("k")).toEqual({ threads: 25, chat: 75 });
    window.localStorage.setItem("bad", "[1,2]");
    expect(readStoredLayout("bad")).toBeNull();
    window.localStorage.setItem("bad2", '{"a": "x"}');
    expect(readStoredLayout("bad2")).toBeNull();
    window.localStorage.setItem("bad3", "{not json");
    expect(readStoredLayout("bad3")).toBeNull();

    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(readStoredLayout("k")).toBeNull();
    expect(() => writeStoredLayout("k", { a: 50, b: 50 })).not.toThrow();
    getItem.mockRestore();
    setItem.mockRestore();
  });

  it("restores the stored layout on mount and labels the handle", async () => {
    writeStoredLayout("studio.test-layout", { left: 30, right: 70 });
    render(
      <div style={{ width: 1000, height: 400 }}>
        <ResizableGroup storageKey="studio.test-layout" defaultLayout={{ left: 50, right: 50 }}>
          <ResizablePanel id="left">Left</ResizablePanel>
          <ResizableHandle label="Resize left panel" />
          <ResizablePanel id="right">Right</ResizablePanel>
        </ResizableGroup>
      </div>,
    );
    const handle = screen.getByRole("separator", { name: "Resize left panel" });
    expect(handle).toBeInTheDocument();
    const left = document.querySelector<HTMLElement>("[data-panel][id='left']");
    expect(left?.style.flexGrow).toBe("30");
  });
});

describe("Tooltip", () => {
  it("shows the label and shortcut on focus and hides on Escape", async () => {
    render(<IconButton label="Toggle sidebar" shortcut="[" icon={<AgentsIcon />} />, { wrapper: TooltipProvider });
    const button = screen.getByRole("button", { name: "Toggle sidebar" });
    act(() => button.focus());
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("Toggle sidebar");
    expect(tooltip).toHaveTextContent("[");
    // The text repeats the button's name, so it is not also wired as its description.
    expect(button).not.toHaveAttribute("aria-describedby");
    fireEvent.keyDown(button, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
  });

  it("describes the trigger when the text adds something, and keeps the trigger's own description", async () => {
    render(
      <>
        <Tooltip content="Copies the id to the clipboard">
          <button type="button" aria-describedby="own-hint">Copy</button>
        </Tooltip>
        <p id="own-hint">Run id</p>
      </>,
      { wrapper: TooltipProvider },
    );
    const button = screen.getByRole("button", { name: "Copy" });
    act(() => button.focus());
    await screen.findByRole("tooltip");
    const ids = button.getAttribute("aria-describedby")?.split(" ") ?? [];
    expect(ids).toContain("own-hint");
    expect(ids.length).toBe(2);
  });
});
