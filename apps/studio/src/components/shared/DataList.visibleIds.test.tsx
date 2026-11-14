import { describe, expect, it, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import { DataList, type DataListColumn } from "./DataList";

interface Row { id: string; name: string; n: number }
const rows: Row[] = [
  { id: "a", name: "Beta", n: 2 },
  { id: "b", name: "Alpha", n: 3 },
  { id: "c", name: "Gamma", n: 1 },
];
const columns: DataListColumn<Row>[] = [
  { id: "name", header: "Name", cell: (r) => r.name, sortable: true },
  { id: "n", header: "N", cell: (r) => r.n, sortable: true },
];

describe("DataList onVisibleRowIdsChange (SL.12: a Sheet's prev / next follow the shown order)", () => {
  it("reports the ids as shown, again after a sort change, and not on a re-render with the same order", () => {
    const seen = vi.fn();
    const view = render(<DataList label="Rows" rows={rows} columns={columns} getRowId={(r) => r.id} onVisibleRowIdsChange={seen} />);
    expect(seen).toHaveBeenLastCalledWith(["a", "b", "c"]);
    fireEvent.click(screen.getByRole("button", { name: "Name" }));
    expect(seen).toHaveBeenLastCalledWith(["b", "a", "c"]);
    fireEvent.click(screen.getByRole("button", { name: "Name" }));
    expect(seen).toHaveBeenLastCalledWith(["c", "a", "b"]);
    const calls = seen.mock.calls.length;
    view.rerender(<DataList label="Rows" rows={rows} columns={columns} getRowId={(r) => r.id} onVisibleRowIdsChange={seen} defaultSort={null} />);
    expect(seen.mock.calls.length).toBe(calls);
  });

  it("reports the searched subset", async () => {
    const seen = vi.fn();
    render(
      <DataList label="Rows" rows={rows} columns={columns} getRowId={(r) => r.id} onVisibleRowIdsChange={seen}
        searchPlaceholder="Search" filterRow={(r, q) => r.name.toLowerCase().includes(q.toLowerCase())} />,
    );
    fireEvent.change(screen.getByRole("searchbox", { name: "Search" }), { target: { value: "a" } });
    await new Promise((resolve) => setTimeout(resolve, 350));
    await screen.findAllByRole("row");
    expect(seen).toHaveBeenLastCalledWith(["a", "b", "c"]);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search" }), { target: { value: "gam" } });
    await new Promise((resolve) => setTimeout(resolve, 350));
    await screen.findByText("Gamma");
    expect(seen).toHaveBeenLastCalledWith(["c"]);
  });
});
