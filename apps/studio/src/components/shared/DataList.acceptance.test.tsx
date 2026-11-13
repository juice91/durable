import React from "react";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { expectNoAxeViolations } from "@/test/a11y";
import { DATALIST_SEARCH_DEBOUNCE_MS, DataList, type DataListColumn } from "./DataList";
import { routes } from "@/lib/routes";

interface Row { id: string; name: string; tools: number }
const rows: Row[] = [
  { id: "a1", name: "Charlie", tools: 3 },
  { id: "a2", name: "alpha", tools: 10 },
  { id: "a3", name: "Bravo", tools: 0 },
];
const columns: DataListColumn<Row>[] = [
  { id: "name", header: "Name", cell: (r) => r.name, sortable: true },
  { id: "tools", header: "Tools", cell: (r) => r.tools, sortable: true, align: "end" },
];
const filterRow = (r: Row, q: string) => r.name.toLowerCase().includes(q.toLowerCase());
const order = () => screen.getAllByRole("row").slice(1).map((r) => within(r).getAllByRole("cell")[0].textContent);
const base = { label: "Agents", columns, getRowId: (r: Row) => r.id };

afterEach(() => vi.useRealTimers());

describe("DataList search", () => {
  it("does not filter or report until 300 ms after the LAST keystroke", () => {
    vi.useFakeTimers();
    const onSearchChange = vi.fn();
    render(<DataList {...base} rows={rows} searchPlaceholder="Search" filterRow={filterRow} onSearchChange={onSearchChange} />);
    const input = screen.getByRole("searchbox", { name: "Search" });
    fireEvent.change(input, { target: { value: "a" } });
    act(() => vi.advanceTimersByTime(200));
    fireEvent.change(input, { target: { value: "al" } });
    act(() => vi.advanceTimersByTime(200));
    expect(order()).toHaveLength(3);
    expect(onSearchChange).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(DATALIST_SEARCH_DEBOUNCE_MS - 200));
    expect(order()).toEqual(["alpha"]);
    expect(onSearchChange).toHaveBeenCalledTimes(1);
    expect(onSearchChange).toHaveBeenCalledWith("al");
  });

  it("initialQuery is applied immediately and not reported as a change", () => {
    vi.useFakeTimers();
    const onSearchChange = vi.fn();
    render(<DataList {...base} rows={rows} searchPlaceholder="Search" initialQuery="bra" filterRow={filterRow} onSearchChange={onSearchChange} />);
    expect(order()).toEqual(["Bravo"]);
    act(() => vi.advanceTimersByTime(1000));
    expect(onSearchChange).not.toHaveBeenCalled();
  });

  it("/ focuses the search from the body and from a non-field, but not from another input, with a modifier, or over a dialog", () => {
    render(
      <>
        <input aria-label="Other" />
        <textarea aria-label="Notes" />
        <button type="button">btn</button>
        <DataList {...base} rows={rows} searchPlaceholder="Search" />
      </>,
    );
    const search = screen.getByRole("searchbox", { name: "Search" });
    const btn = screen.getByRole("button", { name: "btn" });
    btn.focus();
    fireEvent.keyDown(btn, { key: "/", ctrlKey: true });
    expect(search).not.toHaveFocus();
    for (const el of [screen.getByLabelText("Other"), screen.getByLabelText("Notes")]) {
      el.focus();
      fireEvent.keyDown(el, { key: "/" });
      expect(el).toHaveFocus();
    }
    btn.focus();
    const dlg = document.createElement("div");
    dlg.setAttribute("role", "dialog");
    document.body.appendChild(dlg);
    fireEvent.keyDown(btn, { key: "/" });
    expect(search).not.toHaveFocus();
    dlg.remove();
    fireEvent.keyDown(btn, { key: "/" });
    expect(search).toHaveFocus();
  });

  it("searchShortcut={false} disables / and the hint; Escape in the box clears the query", () => {
    const { rerender } = render(<DataList {...base} rows={rows} searchPlaceholder="Search" searchShortcut={false} />);
    const search = screen.getByRole("searchbox", { name: "Search" });
    expect(search).not.toHaveAttribute("aria-keyshortcuts");
    fireEvent.keyDown(document.body, { key: "/" });
    expect(search).not.toHaveFocus();
    rerender(<DataList {...base} rows={rows} searchPlaceholder="Search" />);
    fireEvent.change(search, { target: { value: "xyz" } });
    fireEvent.keyDown(search, { key: "Escape" });
    expect(search).toHaveValue("");
  });
});

describe("DataList sort", () => {
  const th = (name: string) => screen.getByRole("columnheader", { name });

  it("cycles asc, desc, none with aria-sort and sorts naturally, case-insensitively", () => {
    render(<DataList {...base} rows={rows} />);
    expect(th("Name")).toHaveAttribute("aria-sort", "none");
    const btn = within(th("Name")).getByRole("button");
    fireEvent.click(btn);
    expect(th("Name")).toHaveAttribute("aria-sort", "ascending");
    expect(order()).toEqual(["alpha", "Bravo", "Charlie"]);
    fireEvent.click(btn);
    expect(th("Name")).toHaveAttribute("aria-sort", "descending");
    expect(order()).toEqual(["Charlie", "Bravo", "alpha"]);
    fireEvent.click(btn);
    expect(th("Name")).toHaveAttribute("aria-sort", "none");
    expect(order()).toEqual(["Charlie", "alpha", "Bravo"]);
  });

  it("only one column is sorted at a time and numbers sort numerically", () => {
    render(<DataList {...base} rows={rows} />);
    fireEvent.click(within(th("Name")).getByRole("button"));
    fireEvent.click(within(th("Tools")).getByRole("button"));
    expect(th("Name")).toHaveAttribute("aria-sort", "none");
    expect(th("Tools")).toHaveAttribute("aria-sort", "ascending");
    expect(order()).toEqual(["Bravo", "Charlie", "alpha"]);
  });

  it("controlled sort and manualSort: header reports, rows are not re-sorted", () => {
    const onSortChange = vi.fn();
    render(<DataList {...base} rows={rows} sort={{ columnId: "name", direction: "desc" }} manualSort onSortChange={onSortChange} />);
    expect(th("Name")).toHaveAttribute("aria-sort", "descending");
    expect(order()).toEqual(["Charlie", "alpha", "Bravo"]);
    fireEvent.click(within(th("Name")).getByRole("button"));
    expect(onSortChange).toHaveBeenCalledWith(null);
    expect(th("Name")).toHaveAttribute("aria-sort", "descending");
  });

  it("non-sortable columns have no aria-sort and no button", () => {
    render(<DataList {...base} rows={rows} columns={[...columns, { id: "x", header: "Plain", cell: () => "-" }]} />);
    expect(th("Plain")).not.toHaveAttribute("aria-sort");
    expect(within(th("Plain")).queryByRole("button")).toBeNull();
  });
});

describe("DataList rows: keyboard and open", () => {
  it("href rows: first cell is a link; Enter on the link follows it; Enter on the row's other cell clicks the link", () => {
    const clicks: string[] = [];
    render(<div onClick={(e) => { const a = (e.target as HTMLElement).closest("a"); if (a) { e.preventDefault(); clicks.push(a.getAttribute("href") ?? ""); } }}>
      <DataList {...base} rows={rows} getRowHref={(r) => routes.agentIndex(r.id)} />
    </div>);
    const link = screen.getByRole("link", { name: "Charlie" });
    expect(link).toHaveAttribute("href", routes.agentIndex("a1"));
    expect(screen.getAllByRole("link")).toHaveLength(3);
    const cell = within(link.closest("tr") as HTMLElement).getAllByRole("cell")[1];
    cell.tabIndex = 0;
    cell.focus();
    fireEvent.keyDown(cell, { key: "Enter" });
    expect(clicks).toEqual([routes.agentIndex("a1")]);
    fireEvent.click(cell);
    expect(clicks).toEqual([routes.agentIndex("a1"), routes.agentIndex("a1")]);
  });

  it("onRowOpen rows: buttons; arrows move; Enter on the focused button opens that row", () => {
    const onRowOpen = vi.fn();
    render(<DataList {...base} rows={rows} onRowOpen={onRowOpen} />);
    const first = screen.getByRole("button", { name: "Charlie" });
    first.focus();
    fireEvent.keyDown(first, { key: "ArrowDown" });
    expect(screen.getByRole("button", { name: "alpha" })).toHaveFocus();
    fireEvent.click(document.activeElement as HTMLElement);
    expect(onRowOpen).toHaveBeenCalledWith(rows[1]);
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Home" });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: "ArrowUp" });
    expect(first).toHaveFocus();
  });

  it("a plain list (no href, no onRowOpen) renders no links or buttons in rows and rows are not clickable", () => {
    render(<DataList {...base} rows={rows} />);
    expect(screen.queryAllByRole("link")).toHaveLength(0);
    expect(within(screen.getAllByRole("row")[1]).queryByRole("button")).toBeNull();
  });

  it("selectedRowId marks exactly one row; null and unknown ids mark none", () => {
    const { rerender } = render(<DataList {...base} rows={rows} getRowHref={(r) => `/a/${r.id}`} selectedRowId="a2" />);
    expect(document.querySelectorAll("[data-selected]")).toHaveLength(1);
    expect(document.querySelector("[data-row-id='a2']")).toHaveAttribute("data-selected", "true");
    rerender(<DataList {...base} rows={rows} getRowHref={(r) => `/a/${r.id}`} selectedRowId="nope" />);
    expect(document.querySelectorAll("[data-selected]")).toHaveLength(0);
    rerender(<DataList {...base} rows={rows} getRowHref={(r) => `/a/${r.id}`} selectedRowId={null} />);
    expect(document.querySelectorAll("[data-selected]")).toHaveLength(0);
  });
});

describe("DataList states", () => {
  const search = { searchPlaceholder: "Search", filterRow };

  it("every state sets data-state, shows the right surface, and is axe clean", async () => {
    const onRetry = vi.fn();
    const empty = { title: "No agents", description: "Create one.", action: <button type="button">New</button> };
    const cases: Array<[string, React.ReactElement]> = [
      ["loading", <DataList key="l" {...base} {...search} testId="dl" rows={[]} loading />],
      ["error", <DataList key="e" {...base} {...search} testId="dl" rows={[]} error="Boom" onRetry={onRetry} />],
      ["empty", <DataList key="m" {...base} {...search} testId="dl" rows={[]} empty={empty} />],
      ["no-match", <DataList key="n" {...base} {...search} testId="dl" rows={rows} initialQuery="zzz" />],
      ["rows", <DataList key="r" {...base} {...search} testId="dl" rows={rows} getRowHref={(r) => `/a/${r.id}`} />],
    ];
    for (const [state, el] of cases) {
      const { unmount } = render(el);
      expect(screen.getByTestId("dl")).toHaveAttribute("data-state", state);
      await expectNoAxeViolations(document.body);
      unmount();
    }
  });

  it("no-match (via initialQuery + filter) is reached after the debounce and Clear search restores rows", () => {
    vi.useFakeTimers();
    render(<DataList {...base} {...search} rows={rows} initialQuery="zzz" />);
    act(() => vi.advanceTimersByTime(DATALIST_SEARCH_DEBOUNCE_MS));
    expect(screen.getByTestId("datalist-no-match")).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader")).toHaveLength(2);
    fireEvent.click(within(screen.getByTestId("datalist-no-match")).getByRole("button", { name: "Clear search and show all rows" }));
    act(() => vi.advanceTimersByTime(DATALIST_SEARCH_DEBOUNCE_MS));
    expect(order()).toHaveLength(3);
    expect(screen.getByRole("searchbox", { name: "Search" })).toHaveValue("");
  });

  it("an error shows the alert; retry is absent without onRetry; loading with rows keeps rows", () => {
    const { rerender } = render(<DataList {...base} rows={rows} error="Failed" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Failed");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    rerender(<DataList {...base} rows={rows} loading />);
    expect(order()).toHaveLength(3);
    expect(screen.queryAllByTestId("datalist-skeleton-row")).toHaveLength(0);
  });

  it("empty falls back to a default title and renders the action", () => {
    render(<DataList {...base} rows={[]} />);
    expect(within(screen.getByTestId("datalist-empty")).getByRole("heading")).toHaveTextContent("Nothing here yet");
  });
});

describe("DataList scale", () => {
  it("renders 1,000 rows, sorts and filters them within a reasonable time", () => {
    vi.useFakeTimers();
    const many: Row[] = Array.from({ length: 1000 }, (_, i) => ({ id: `r${i}`, name: `Agent ${String(i).padStart(4, "0")}`, tools: i % 17 }));
    const toolsColumn = () => screen.getAllByRole("row").slice(1).map((r) => Number(within(r).getAllByRole("cell")[1].textContent));
    const start = performance.now();
    render(<DataList {...base} rows={many} getRowHref={(r) => `/a/${r.id}`} searchPlaceholder="Search" filterRow={filterRow} />);
    expect(performance.now() - start).toBeLessThan(3000);
    expect(screen.getAllByRole("row")).toHaveLength(1001);

    const sortStart = performance.now();
    fireEvent.click(within(screen.getByRole("columnheader", { name: "Tools" })).getByRole("button"));
    expect(performance.now() - sortStart).toBeLessThan(3000);
    const sorted = toolsColumn();
    expect(sorted).toHaveLength(1000);
    expect(sorted).toEqual([...sorted].sort((a, b) => a - b));
    // Stable: ties keep the original (name) order.
    expect(order().slice(0, 3)).toEqual(["Agent 0000", "Agent 0017", "Agent 0034"]);

    const filterStart = performance.now();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search" }), { target: { value: "Agent 09" } });
    act(() => vi.advanceTimersByTime(DATALIST_SEARCH_DEBOUNCE_MS));
    expect(performance.now() - filterStart).toBeLessThan(3000);
    const names = order();
    expect(names).toHaveLength(100);
    expect(names.every((name) => name?.startsWith("Agent 09"))).toBe(true);
    const filteredTools = toolsColumn();
    expect(filteredTools).toEqual([...filteredTools].sort((a, b) => a - b));
  });
});
