// Acceptance (browser check finding B): DataList at 375 px has no overflow, checked by class (jsdom has
// no layout): the scroll wrapper is the containing block of every absolutely positioned descendant, it
// clips (overflow-x-auto) and shrinks (min-w-0); every page that uses DataList passes the phone layout guard.
import fs from "node:fs";
import path from "node:path";
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { checkLayout } from "@/test/layoutGuard";
import { DataList, type DataListColumn } from "./DataList";

const SRC = path.resolve(__dirname, "../..");
const USERS = [
  "app/knowledge/page.tsx",
  "app/mcp/[id]/page.tsx",
  "app/mcp/page.tsx",
  "app/tools/page.tsx",
  "app/traces/page.tsx",
  "app/workflows/[id]/traces/page.tsx",
  "app/workflows/page.tsx",
  "components/agentPage/TracesTab.tsx",
  "components/agents/AgentsList.tsx",
  "components/knowledge/DocumentsTab.tsx",
  "components/knowledge/IngestRunsTab.tsx",
  "components/logs/LogsList.tsx",
  "components/processors/ProcessorList.tsx",
];

function sources(dir: string, out: string[] = []): string[] {
  for (const e of fs.readdirSync(path.join(SRC, dir), { withFileTypes: true })) {
    const rel = `${dir}/${e.name}`;
    if (e.isDirectory()) sources(rel, out);
    else if (/\.tsx$/.test(e.name) && !/\.test\.tsx$/.test(e.name)) out.push(rel);
  }
  return out;
}

describe("DataList on a phone", () => {
  const users = [...sources("app"), ...sources("components")]
    .filter((f) => /<DataList\b/.test(fs.readFileSync(path.join(SRC, f), "utf8")) && f !== "components/shared/DataList.tsx")
    .sort();

  it("the set of pages that use DataList is the one this test covers", () => {
    expect(users).toEqual([...USERS].sort());
  });

  it.each(USERS)("%s passes the phone layout guard and has no table of its own", (file) => {
    const source = fs.readFileSync(path.join(SRC, file), "utf8");
    expect(checkLayout(file, source)).toEqual([]);
    expect(source).not.toMatch(/<table[\s>]/);
  });

  it("every sr-only or absolute descendant is inside the positioned, clipping scroll wrapper", () => {
    type Row = { id: string };
    const columns: DataListColumn<Row>[] = [
      { id: "name", header: "Name", cell: (r) => r.id, sortable: true },
      { id: "wide", header: "Wide", cell: () => <span className="whitespace-nowrap">{"x".repeat(400)}</span> },
      { id: "actions", header: <span className="sr-only">Actions</span>, cell: () => <button type="button">Open</button>, align: "end" },
    ];
    render(<DataList label="Docs" rows={[{ id: "a" }, { id: "b" }]} columns={columns} getRowId={(r) => r.id} />);
    const scroll = screen.getByTestId("datalist-scroll");
    const hidden = screen.getByText("Actions");
    // Walk up from the sr-only header: the first positioned ancestor must be the scroll wrapper.
    let node: HTMLElement | null = hidden.parentElement;
    let positioned: HTMLElement | null = null;
    while (node && !positioned) {
      if (/(^|\s)(relative|absolute|fixed|sticky)(\s|$)/.test(node.className)) positioned = node;
      node = node.parentElement;
    }
    expect(positioned).toBe(scroll);
    const classes = scroll.className.split(/\s+/);
    expect(classes).toEqual(expect.arrayContaining(["relative", "overflow-x-auto", "min-w-0"]));
    expect(classes.some((c) => /^(w-screen|min-w-\[|w-\[\d+)/.test(c))).toBe(false);
    expect(scroll.parentElement?.className ?? "").not.toMatch(/overflow-visible/);
  });
});
