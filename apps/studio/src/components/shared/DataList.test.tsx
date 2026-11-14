import React, { useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/Popover";
import { ConfirmDialog } from "./ConfirmDialog";
import { DATALIST_SEARCH_DEBOUNCE_MS, DataList, compareSortValues, nextSort, type DataListColumn } from "./DataList";
import { routes } from "@/lib/routes";

interface Agent {
  id: string;
  name: string;
  model: string;
  tools: number;
}

const agents: Agent[] = [
  { id: "a1", name: "Support bot", model: "gpt-4o", tools: 3 },
  { id: "a2", name: "Billing helper", model: "claude", tools: 10 },
  { id: "a3", name: "Archivist", model: "gpt-4o-mini", tools: 0 },
];

const columns: DataListColumn<Agent>[] = [
  { id: "name", header: "Name", cell: (row) => row.name, sortable: true },
  { id: "model", header: "Model", cell: (row) => row.model },
  { id: "tools", header: "Tools", cell: (row) => row.tools, sortable: true, align: "end" },
];

function names() {
  return screen.getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell")[0].textContent);
}

afterEach(() => {
  vi.useRealTimers();
});

describe("sort helpers", () => {
  it("cycles ascending, descending, unsorted and compares naturally with empties last", () => {
    expect(nextSort(null, "name")).toEqual({ columnId: "name", direction: "asc" });
    expect(nextSort({ columnId: "name", direction: "asc" }, "name")).toEqual({ columnId: "name", direction: "desc" });
    expect(nextSort({ columnId: "name", direction: "desc" }, "name")).toBeNull();
    expect(nextSort({ columnId: "name", direction: "desc" }, "tools")).toEqual({ columnId: "tools", direction: "asc" });
    expect(compareSortValues("v2", "v10")).toBeLessThan(0);
    expect(compareSortValues(null, "a")).toBeGreaterThan(0);
    expect(compareSortValues(2, 10)).toBeLessThan(0);
  });
});

describe("DataList", () => {
  it("renders an accessible table with link rows and sortable headers that toggle aria-sort", async () => {
    render(<DataList label="Agents" rows={agents} columns={columns} getRowId={(row) => row.id} getRowHref={(row) => routes.agentChat(row.id)} />);
    const table = screen.getByRole("table", { name: "Agents" });
    expect(within(table).getByRole("link", { name: "Support bot" })).toHaveAttribute("href", routes.agentChat("a1"));

    const nameHeader = screen.getByRole("columnheader", { name: /Name/ });
    expect(nameHeader).toHaveAttribute("aria-sort", "none");
    expect(screen.getByRole("columnheader", { name: "Model" })).not.toHaveAttribute("aria-sort");

    fireEvent.click(within(nameHeader).getByRole("button"));
    expect(nameHeader).toHaveAttribute("aria-sort", "ascending");
    expect(names()).toEqual(["Archivist", "Billing helper", "Support bot"]);

    fireEvent.click(within(nameHeader).getByRole("button"));
    expect(nameHeader).toHaveAttribute("aria-sort", "descending");
    expect(names()).toEqual(["Support bot", "Billing helper", "Archivist"]);

    fireEvent.click(within(nameHeader).getByRole("button"));
    expect(nameHeader).toHaveAttribute("aria-sort", "none");
    expect(names()).toEqual(["Support bot", "Billing helper", "Archivist"]);

    const toolsHeader = screen.getByRole("columnheader", { name: /Tools/ });
    fireEvent.click(within(toolsHeader).getByRole("button"));
    expect(names()).toEqual(["Archivist", "Support bot", "Billing helper"]);
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  it("follows the row link when any part of the row is clicked, but not from the row's own controls", () => {
    const onLinkClick = vi.fn((event: MouseEvent) => event.preventDefault());
    const onAction = vi.fn();
    const withAction: DataListColumn<Agent>[] = [
      ...columns,
      { id: "actions", header: "Actions", cell: (row) => <button type="button" onClick={() => onAction(row.id)}>Delete {row.name}</button> },
    ];
    render(<DataList label="Agents" rows={agents} columns={withAction} getRowId={(row) => row.id} getRowHref={(row) => routes.agentChat(row.id)} />);
    const link = screen.getByRole("link", { name: "Billing helper" });
    link.addEventListener("click", onLinkClick);

    fireEvent.click(screen.getByText("claude"));
    expect(onLinkClick).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Delete Billing helper" }));
    expect(onAction).toHaveBeenCalledWith("a2");
    expect(onLinkClick).toHaveBeenCalledTimes(1);
  });

  it("opens rows in place with onRowOpen and marks the selected row", () => {
    const onRowOpen = vi.fn();
    render(<DataList label="Tools" rows={agents} columns={columns} getRowId={(row) => row.id} onRowOpen={onRowOpen} selectedRowId="a3" />);
    fireEvent.click(screen.getByText("gpt-4o"));
    expect(onRowOpen).toHaveBeenCalledWith(agents[0]);
    fireEvent.click(screen.getByRole("button", { name: "Archivist" }));
    expect(onRowOpen).toHaveBeenLastCalledWith(agents[2]);
    const selectedRow = screen.getByRole("button", { name: "Archivist" }).closest("tr");
    expect(selectedRow).toHaveAttribute("data-selected", "true");
    expect(selectedRow).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("button", { name: "Support bot" }).closest("tr")).not.toHaveAttribute("aria-current");
  });

  it("ignores clicks inside a portaled dialog opened from a row (its text and backdrop)", async () => {
    function DeleteCell({ row }: { row: Agent }) {
      const [asking, setAsking] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setAsking(true)}>Delete {row.name}</button>
          {asking ? <ConfirmDialog title={`Delete ${row.name}?`} description="This cannot be undone." confirmLabel="Delete" onConfirm={() => setAsking(false)} onCancel={() => setAsking(false)} /> : null}
        </>
      );
    }
    const withDelete: DataListColumn<Agent>[] = [...columns, { id: "actions", header: "Actions", cell: (row) => <DeleteCell row={row} /> }];
    const onRowOpen = vi.fn();
    const onLinkClick = vi.fn((event: MouseEvent) => event.preventDefault());
    const { unmount } = render(<DataList label="Tools" rows={agents} columns={withDelete} getRowId={(row) => row.id} onRowOpen={onRowOpen} />);
    fireEvent.click(screen.getByRole("button", { name: "Delete Billing helper" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete Billing helper?" });
    fireEvent.click(within(dialog).getByText("This cannot be undone."));
    fireEvent.click(dialog);
    const backdrop = dialog.closest("[data-confirm-dialog-root]")?.querySelector("[data-state]:not([role='dialog'])");
    expect(backdrop).toBeTruthy();
    fireEvent.click(backdrop as HTMLElement);
    expect(onRowOpen).not.toHaveBeenCalled();
    unmount();

    render(<DataList label="Agents" rows={agents} columns={withDelete} getRowId={(row) => row.id} getRowHref={(row) => routes.agentChat(row.id)} />);
    screen.getAllByRole("link").forEach((link) => link.addEventListener("click", onLinkClick));
    fireEvent.click(screen.getByRole("button", { name: "Delete Archivist" }));
    const second = await screen.findByRole("dialog", { name: "Delete Archivist?" });
    fireEvent.click(within(second).getByText("This cannot be undone."));
    fireEvent.click(second.closest("[data-confirm-dialog-root]")?.querySelector("[data-state]:not([role='dialog'])") as HTMLElement);
    expect(onLinkClick).not.toHaveBeenCalled();
  });

  it("leaves arrow, Home and End inside a portaled popover in a row to the popover", async () => {
    const withMenu: DataListColumn<Agent>[] = [
      ...columns,
      {
        id: "more",
        header: "More",
        cell: (row) => (
          <Popover>
            <PopoverTrigger asChild>
              <button type="button">Options for {row.name}</button>
            </PopoverTrigger>
            <PopoverContent aria-label={`Options for ${row.name}`}>
              <button type="button">First option</button>
              <button type="button">Last option</button>
            </PopoverContent>
          </Popover>
        ),
      },
    ];
    render(<DataList label="Agents" rows={agents} columns={withMenu} getRowId={(row) => row.id} getRowHref={(row) => routes.agentChat(row.id)} />);
    fireEvent.click(screen.getByRole("button", { name: "Options for Archivist" }));
    const popover = await screen.findByRole("dialog", { name: "Options for Archivist" });
    const last = within(popover).getByRole("button", { name: "Last option" });
    act(() => last.focus());
    for (const key of ["ArrowDown", "ArrowUp", "Home", "End"]) {
      fireEvent.keyDown(last, { key });
      expect(last).toHaveFocus();
    }
    expect(screen.getByRole("link", { name: "Support bot" })).not.toHaveFocus();
    await waitFor(() => expect(popover).toBeInTheDocument());
  });

  it("moves between rows with ArrowUp, ArrowDown, Home and End, and from the search box", () => {
    render(
      <DataList
        label="Agents"
        rows={agents}
        columns={columns}
        getRowId={(row) => row.id}
        getRowHref={(row) => routes.agentChat(row.id)}
        searchPlaceholder="Search agents"
        filterRow={(row, query) => row.name.toLowerCase().includes(query.toLowerCase())}
      />,
    );
    const search = screen.getByRole("searchbox", { name: "Search agents" });
    search.focus();
    fireEvent.keyDown(search, { key: "ArrowDown" });
    const first = screen.getByRole("link", { name: "Support bot" });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: "ArrowDown" });
    expect(screen.getByRole("link", { name: "Billing helper" })).toHaveFocus();
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "End" });
    expect(screen.getByRole("link", { name: "Archivist" })).toHaveFocus();
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowDown" });
    expect(screen.getByRole("link", { name: "Archivist" })).toHaveFocus();
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowUp" });
    expect(screen.getByRole("link", { name: "Billing helper" })).toHaveFocus();
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Home" });
    expect(first).toHaveFocus();
  });

  it("debounces the search by 300 ms, reports the query, and shows no-match with Clear search", () => {
    vi.useFakeTimers();
    const onSearchChange = vi.fn();
    render(
      <DataList
        label="Agents"
        rows={agents}
        columns={columns}
        getRowId={(row) => row.id}
        searchPlaceholder="Search agents"
        onSearchChange={onSearchChange}
        filterRow={(row, query) => row.name.toLowerCase().includes(query.toLowerCase())}
      />,
    );
    const search = screen.getByRole("searchbox", { name: "Search agents" });
    fireEvent.change(search, { target: { value: "bill" } });
    act(() => vi.advanceTimersByTime(DATALIST_SEARCH_DEBOUNCE_MS - 1));
    expect(names()).toHaveLength(3);
    expect(onSearchChange).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(names()).toEqual(["Billing helper"]);
    expect(onSearchChange).toHaveBeenCalledWith("bill");

    fireEvent.change(search, { target: { value: "zzz" } });
    act(() => vi.advanceTimersByTime(DATALIST_SEARCH_DEBOUNCE_MS));
    expect(screen.getByTestId("datalist-no-match")).toHaveTextContent("No matching results.");
    fireEvent.click(within(screen.getByTestId("datalist-no-match")).getByRole("button", { name: "Clear search and show all rows" }));
    expect(search).toHaveFocus();
    expect(search).toHaveValue("");
    act(() => vi.advanceTimersByTime(DATALIST_SEARCH_DEBOUNCE_MS));
    expect(names()).toHaveLength(3);
  });

  it("focuses the search box on / outside fields only", () => {
    render(
      <>
        <input aria-label="Other field" />
        <DataList label="Agents" rows={agents} columns={columns} getRowId={(row) => row.id} searchPlaceholder="Search agents" />
      </>,
    );
    const search = screen.getByRole("searchbox", { name: "Search agents" });
    expect(search).toHaveAttribute("aria-keyshortcuts", "/");
    const other = screen.getByRole("textbox", { name: "Other field" });
    other.focus();
    fireEvent.keyDown(other, { key: "/" });
    expect(other).toHaveFocus();
    other.blur();
    fireEvent.keyDown(document.body, { key: "/" });
    expect(search).toHaveFocus();
  });

  it("walks the state ladder: loading, error with retry, empty with action", async () => {
    const onRetry = vi.fn();
    const { rerender } = render(<DataList label="Agents" rows={[]} columns={columns} getRowId={(row) => row.id} loading />);
    expect(screen.getAllByTestId("datalist-skeleton-row")).toHaveLength(5);
    expect(screen.getByRole("table", { name: "Agents" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("status")).toHaveTextContent("Loading agents");

    rerender(<DataList label="Agents" rows={[]} columns={columns} getRowId={(row) => row.id} error="API unavailable" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("API unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalled();

    rerender(
      <DataList
        label="Agents"
        rows={[]}
        columns={columns}
        getRowId={(row) => row.id}
        empty={{ title: "No agents yet", description: "Create one to start chatting.", action: <button type="button">New agent</button> }}
      />,
    );
    const emptyState = screen.getByTestId("datalist-empty");
    expect(within(emptyState).getByRole("heading", { name: "No agents yet" })).toBeInTheDocument();
    expect(within(emptyState).getByRole("button", { name: "New agent" })).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
