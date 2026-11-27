// The workflow page's draft notice, Review changes and confirmed publish.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom";

vi.mock("@/components/ui/Toaster", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import type { Workflow, WorkflowSpec, WorkflowVersion } from "@/lib/workflows";
import { draftDiffers, summarizeChanges } from "@/lib/workflowDraft";
import { WorkflowDraftBar } from "./WorkflowDraftBar";

const fetchMock = vi.fn();
const json = (status: number, body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));

const spec = (over: Record<string, unknown> = {}): WorkflowSpec =>
  ({
    spec_version: 1,
    name: "Posts",
    input_schema: { type: "object" },
    start: "fetch_posted",
    steps: [
      { id: "fetch_posted", type: "tool", tool_id: "http.request", next: "ignore_gate" },
      { id: "ignore_gate", type: "branch", cases: [], default: "fetch_posted" },
    ],
    ...over,
  }) as unknown as WorkflowSpec;

const version = (s: WorkflowSpec, v = 3): WorkflowVersion => ({ workflow_id: "w1", version: v, spec: s, spec_hash: "h", created_at: "2026-11-20T10:00:00Z", created_by: "u" });
const wf = (draft: WorkflowSpec, over: Partial<Workflow> = {}): Workflow => ({ id: "w1", name: "Posts", draft_spec: draft, latest_version: 3, revision: 5, archived: false, ...over });

const changedDraft = () =>
  spec({
    steps: [
      { id: "fetch_posted", type: "tool", tool_id: "http.request", next: "ignore_gate" },
      { id: "ignore_gate", type: "branch", cases: [], default: "cap_history_24h" },
      { id: "cap_history_24h", type: "tool", tool_id: "http.request", next: null },
    ],
  });

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
});

describe("draft comparison", () => {
  it("ignores key order", () => {
    const a = spec();
    const reordered = JSON.parse(JSON.stringify({ steps: a.steps.map((s) => Object.fromEntries(Object.entries(s).reverse())), start: a.start, input_schema: a.input_schema, name: a.name, spec_version: 1 }));
    expect(draftDiffers(reordered, a)).toBe(false);
    expect(draftDiffers(changedDraft(), a)).toBe(true);
  });
  it("lists added, removed and changed steps", () => {
    const c = summarizeChanges(spec(), changedDraft());
    expect(c.added).toEqual(["cap_history_24h"]);
    expect(c.removed).toEqual([]);
    expect(c.changed).toEqual([{ id: "ignore_gate", fields: ["default fetch_posted → cap_history_24h"] }]);
    expect(summarizeChanges(changedDraft(), spec()).removed).toEqual(["cap_history_24h"]);
  });
});

describe("WorkflowDraftBar", () => {
  it("shows no notice when the draft equals the live spec in another key order", () => {
    const live = spec();
    const same = JSON.parse(JSON.stringify(live), (_k, v) => v);
    render(<WorkflowDraftBar workflow={wf({ ...same, steps: same.steps.map((s: object) => Object.fromEntries(Object.entries(s).reverse())) })} live={version(live)} onChanged={vi.fn()} />);
    expect(screen.queryByTestId("workflow-draft-notice")).toBeNull();
  });

  it("says Not published yet when there is no version", () => {
    render(<WorkflowDraftBar workflow={wf(spec(), { latest_version: null })} live={null} onChanged={vi.fn()} />);
    expect(screen.getByTestId("workflow-draft-notice")).toHaveTextContent("Not published yet");
    expect(screen.getByTestId("workflow-publish")).toHaveTextContent("Publish v1");
  });

  it("shows the notice and the change lists in Review changes, with a raw JSON toggle", async () => {
    render(<WorkflowDraftBar workflow={wf(changedDraft())} live={version(spec())} onChanged={vi.fn()} />);
    expect(screen.getByTestId("workflow-draft-notice")).toHaveTextContent("This workflow has unpublished changes (draft differs from v3)");
    fireEvent.click(screen.getByTestId("workflow-review-changes"));
    const sheet = await screen.findByTestId("workflow-review-sheet");
    expect(within(sheet).getByTestId("workflow-changes-added")).toHaveTextContent("Added steps: cap_history_24h");
    expect(within(sheet).getByTestId("workflow-changes-changed")).toHaveTextContent("Changed: ignore_gate (default fetch_posted → cap_history_24h)");
    expect(within(sheet).queryByTestId("workflow-raw-diff")).toBeNull();
    fireEvent.click(within(sheet).getByTestId("workflow-raw-toggle"));
    expect(within(sheet).getByTestId("workflow-raw-diff")).toHaveTextContent("cap_history_24h");
  });

  it("does not publish until confirmed, then reports the version and warnings and reloads", async () => {
    const onChanged = vi.fn();
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === "POST") return json(201, { ...version(changedDraft(), 4), warnings: [{ code: "cost_cap_unpriced_model", path: "steps.x", message: "Model is unpriced" }] });
      return json(200, wf(changedDraft()));
    });
    render(<WorkflowDraftBar workflow={wf(changedDraft())} live={version(spec())} onChanged={onChanged} />);
    fireEvent.click(screen.getByTestId("workflow-publish"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Publish v4");
    expect(dialog).toHaveTextContent("Added steps: cap_history_24h");
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("workflow-publish"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Publish v4" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(fetchMock.mock.calls.some(([u, i]) => String(u).endsWith("/versions") && i?.method === "POST")).toBe(true);
    expect(screen.getByTestId("workflow-published-notice")).toHaveTextContent("Published v4");
    expect(screen.getByTestId("publish-warnings")).toHaveTextContent("Model is unpriced");
  });

  it("explains a 409: nothing to publish", async () => {
    const onChanged = vi.fn();
    fetchMock.mockImplementation((_u: string, init?: RequestInit) =>
      init?.method === "POST" ? json(409, { detail: "draft equals latest" }) : json(200, wf(changedDraft())));
    render(<WorkflowDraftBar workflow={wf(changedDraft())} live={version(spec())} onChanged={onChanged} />);
    fireEvent.click(screen.getByTestId("workflow-publish"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Publish v4" }));
    expect(await screen.findByTestId("workflow-publish-notice")).toHaveTextContent("Nothing to publish: the draft is the same as v3.");
    expect(onChanged).toHaveBeenCalled();
  });

  it("explains a 412: someone else changed it", async () => {
    const onChanged = vi.fn();
    fetchMock.mockImplementation((_u: string, init?: RequestInit) =>
      init?.method === "POST"
        ? json(412, { code: "revision_mismatch", detail: "changed", current_revision: 6, current: {} })
        : json(200, wf(changedDraft())));
    render(<WorkflowDraftBar workflow={wf(changedDraft())} live={version(spec())} onChanged={onChanged} />);
    fireEvent.click(screen.getByTestId("workflow-publish"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Publish v4" }));
    expect(await screen.findByTestId("workflow-publish-notice")).toHaveTextContent("Someone else changed this workflow");
    expect(onChanged).toHaveBeenCalled();
  });

  it("refuses to publish a draft edited since the page loaded", async () => {
    const onChanged = vi.fn();
    fetchMock.mockImplementation(() => json(200, wf(changedDraft(), { revision: 6 })));
    render(<WorkflowDraftBar workflow={wf(changedDraft())} live={version(spec())} onChanged={onChanged} />);
    fireEvent.click(screen.getByTestId("workflow-publish"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Publish v4" }));
    expect(await screen.findByTestId("workflow-publish-notice")).toHaveTextContent("Someone else changed this workflow");
    expect(fetchMock.mock.calls.some(([, i]) => i?.method === "POST")).toBe(false);
  });
});
