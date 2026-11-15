/**
 * RM.16 Knowledge pages: the list (rows, cost, create with the model picker), and the knowledge base
 * page (documents with progress and Retry, multi-file upload with refusals, delete with references
 * and the 409, the reindex estimate, the search tester, the ingest runs). The API is an in-memory
 * `fetch` stub; navigation is the shared Next router mock, so `?tab=` and `?doc=` behave as in Next.
 */
import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const params = vi.hoisted(() => ({ id: "kb1" }));
vi.mock("next/navigation", async () => {
  const mod = (await import("@/test/nextRouterMock")).navigationModule;
  return { ...mod, useParams: () => params };
});

import { nextRouter } from "@/test/nextRouterMock";
import { KeyboardHarness, pressKey } from "@/components/shortcuts/shortcutsTesting";
import KnowledgePage from "./page";
import KnowledgeBasePage from "./[id]/page";

const T0 = "2026-11-14T10:00:00Z";

function kb(extra: Record<string, unknown> = {}) {
  return {
    id: "kb1", tenant_id: "t1", name: "Product docs", description: "Manuals", embedding_model: "test:hash", dimensions: 1536,
    chunking: {}, active_generation: 1, last_generation: 1, pending_generation: null, pending_embedding_model: null, pending_dimensions: null,
    status: "ready", error: null, created_at: T0, updated_at: T0, document_count: 2, chunk_count: 9, ...extra,
  };
}

function doc(key: string, extra: Record<string, unknown> = {}) {
  return {
    id: `d-${key}`, tenant_id: "t1", kb_id: "kb1", document_key: key, title: key.replace(/\.\w+$/, ""), source_url: null, mime_type: "text/markdown",
    metadata: {}, content_hash: "h", active_version: 1, latest_version: 1, status: "ready", error: null, chunk_count: 4, last_run_id: `run-${key}`,
    created_at: T0, updated_at: T0, ...extra,
  };
}

const MODELS = { items: [
  { id: "test:hash", provider: "test", name: "hash", dimensions: 1536, wildcard: false },
  { id: "litellm:*", provider: "litellm", name: "*", dimensions: 1024, wildcard: true },
] };

type Handler = (url: URL, init: RequestInit) => { status?: number; body?: unknown } | undefined;
let routesTable: Array<[RegExp, string, Handler]>;
let requests: Array<{ method: string; url: URL; body: unknown }>;

function on(method: string, path: RegExp, handler: Handler) {
  routesTable.unshift([path, method, handler]);
}

beforeEach(() => {
  nextRouter.reset(["/knowledge/kb1"]);
  params.id = "kb1";
  requests = [];
  routesTable = [];
  on("GET", /^\/knowledge-bases$/, () => ({ body: { items: [kb(), kb({ id: "kb2", name: "Handbook", document_count: 0, chunk_count: 0 })] } }));
  on("GET", /^\/knowledge-bases\/kb1$/, () => ({ body: { ...kb(), ingest_tokens: 4200, ingest_cost_usd: "0.0042", ingest_requests: 3 } }));
  on("GET", /^\/knowledge-bases\/kb2$/, () => ({ body: { ...kb({ id: "kb2" }), ingest_tokens: 0, ingest_cost_usd: null, ingest_requests: 1 } }));
  on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => ({ body: { items: [doc("install.md"), doc("billing.md", { status: "failed", error: "embedding_failed: provider down", chunk_count: 0 })], next_after: null } }));
  on("GET", /^\/embedding-models$/, () => ({ body: MODELS }));
  on("GET", /^\/agents$/, () => ({ body: [] }));
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input);
      const path = url.pathname.replace(/^\/api\/v1/, "");
      const method = (init.method ?? "GET").toUpperCase();
      requests.push({ method, url, body: init.body ? JSON.parse(String(init.body)) : undefined });
      if (path.endsWith("/stream")) return new Response(new ReadableStream({ start() {} }), { status: 200 });
      for (const [re, m, handler] of routesTable) {
        if (m === method && re.test(path)) {
          const result = handler(url, init) ?? {};
          const status = result.status ?? 200;
          return new Response(status === 204 ? null : JSON.stringify(result.body ?? {}), { status, headers: { "content-type": "application/json" } });
        }
      }
      return new Response(JSON.stringify({ items: [], next_before: null }), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const sent = (method: string, path: RegExp) => requests.filter((r) => r.method === method && path.test(r.url.pathname.replace(/^\/api\/v1/, "")));

describe("/knowledge", () => {
  it("lists knowledge bases with model, counts, status and ingest cost; a row links to its page", async () => {
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    const rows = await screen.findAllByTestId("knowledge-base-row");
    expect(rows).toHaveLength(2);
    const handbook = rows.find((row) => within(row).queryByText("Handbook"))!;
    const product = rows.find((row) => within(row).queryByText("Product docs"))!;
    expect(within(product).getByRole("link", { name: "Product docs" })).toHaveAttribute("href", "/knowledge/kb1");
    expect(within(product).getByTestId("knowledge-base-sections")).toHaveTextContent("9");
    await waitFor(() => expect(within(product).getByTestId("knowledge-base-cost")).toHaveTextContent("$0.0042"));
    await waitFor(() => expect(within(handbook).getByTestId("knowledge-base-cost")).toHaveTextContent("No price"));
    // The subtitle names every upload format (browser check E).
    expect(screen.getByText(/Upload Markdown, text, HTML, reStructuredText, PDF, YAML or JSON;/)).toBeInTheDocument();
  });

  it("shows the empty state", async () => {
    on("GET", /^\/knowledge-bases$/, () => ({ body: { items: [] } }));
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    expect(await screen.findByText("No knowledge bases yet")).toBeInTheDocument();
  });

  it("c opens New knowledge base; a wildcard model asks for a name; create posts and opens the new page", async () => {
    on("POST", /^\/knowledge-bases$/, () => ({ status: 201, body: kb({ id: "kb9", name: "New one" }) }));
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-base-row");
    act(() => pressKey("c"));
    const dialog = await screen.findByTestId("knowledge-create-dialog");
    const submit = within(dialog).getByTestId("knowledge-create-submit");
    expect(submit).toBeDisabled();
    fireEvent.change(within(dialog).getByTestId("knowledge-create-name"), { target: { value: "  New one " } });
    const picker = within(dialog).getByTestId("knowledge-embedding-model");
    await waitFor(() => expect(picker).not.toBeDisabled());
    fireEvent.change(picker, { target: { value: "litellm:*" } });
    expect(submit).toBeDisabled();
    fireEvent.change(within(dialog).getByTestId("knowledge-embedding-model-name"), { target: { value: "nomic-embed-text" } });
    fireEvent.click(submit);
    await waitFor(() => expect(sent("POST", /^\/knowledge-bases$/)).toHaveLength(1));
    expect(sent("POST", /^\/knowledge-bases$/)[0].body).toEqual({ name: "New one", description: null, embedding_model: "litellm:nomic-embed-text" });
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/knowledge/kb9");
  });
});

async function renderDetail() {
  render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
  await screen.findAllByTestId("knowledge-document-row");
}

describe("/knowledge/:id: a document whose ingest never started", () => {
  const ENQUEUE_FAILED = "EnqueueFailed: the run could not be started (enqueue_failed); retry the same request with the same run_id";
  const run = (id: string, status: string, extra: Record<string, unknown> = {}) => ({
    run_id: id, kind: "kb_ingest", knowledge_base_id: "kb1", agent_id: null, version: null, status, display_status: status, input: "{}",
    output: null, error: null, heartbeat_at: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString(), ...extra,
  });

  it("pending with an enqueue-failed run: says so and offers Retry, which reingests; the handed-off run then hides it", async () => {
    let runStatus = "failed";
    on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => ({ body: { items: [doc("stuck.md", { status: "pending", active_version: null, chunk_count: 0, last_run_id: "rs" })], next_after: null } }));
    on("GET", /^\/runs\/rs$/, () => ({ body: run("rs", runStatus, runStatus === "failed" ? { error: ENQUEUE_FAILED } : {}) }));
    on("POST", /\/documents\/stuck\.md\/reingest$/, () => {
      runStatus = "queued";
      return { status: 202, body: { run_id: "rs", kind: "kb_ingest", knowledge_base_id: "kb1", status: "queued", document: doc("stuck.md", { status: "pending", active_version: null, chunk_count: 0, last_run_id: "rs" }) } };
    });
    await renderDetail();
    expect(await screen.findByTestId("knowledge-document-stuck")).toHaveTextContent("The ingest did not start");
    fireEvent.click(screen.getByTestId("knowledge-document-retry"));
    await waitFor(() => expect(sent("POST", /reingest$/)).toHaveLength(1));
    await waitFor(() => expect(screen.queryByTestId("knowledge-document-retry")).toBeNull());
    expect(screen.queryByTestId("knowledge-document-stuck")).toBeNull();
    expect(sent("GET", /^\/runs\/rs$/).length).toBeGreaterThanOrEqual(2);
  });

  it("pending with a run that is running, or a fresh queued one: no Retry", async () => {
    on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => ({ body: { items: [
      doc("a.md", { status: "pending", last_run_id: "ra" }),
      doc("b.md", { status: "pending", last_run_id: "rb" }),
    ], next_after: null } }));
    on("GET", /^\/runs\/ra$/, () => ({ body: run("ra", "running") }));
    on("GET", /^\/runs\/rb$/, () => ({ body: run("rb", "queued") }));
    await renderDetail();
    await waitFor(() => expect(sent("GET", /^\/runs\/r[ab]$/)).toHaveLength(2));
    expect(screen.queryByTestId("knowledge-document-retry")).toBeNull();
    expect(screen.queryByTestId("knowledge-document-stuck")).toBeNull();
  });

  it("a run queued for over a minute: 'Still queued' and Retry; a 409 'already being ingested' is a quiet note, not an error", async () => {
    on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => ({ body: { items: [doc("q.md", { status: "pending", last_run_id: "rq", updated_at: "2026-01-01T00:00:00Z" })], next_after: null } }));
    on("GET", /^\/runs\/rq$/, () => ({ body: run("rq", "queued", { created_at: "2026-01-01T00:00:00Z" }) }));
    on("POST", /\/documents\/q\.md\/reingest$/, () => ({ status: 409, body: { detail: "the document is already being ingested (run rq is running)" } }));
    await renderDetail();
    expect(await screen.findByTestId("knowledge-document-stuck")).toHaveTextContent("Still queued after a minute. Retry re-queues it.");
    fireEvent.click(screen.getByTestId("knowledge-document-retry"));
    expect(await screen.findByTestId("knowledge-documents-info")).toHaveTextContent("q: the ingest is already running.");
    expect(screen.getByTestId("knowledge-documents-info")).toHaveAttribute("role", "status");
    expect(screen.queryByTestId("knowledge-documents-error")).toBeNull();
  });

  it("another 409 on Retry is still an error", async () => {
    on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => ({ body: { items: [doc("x.md", { status: "pending", last_run_id: "rx" })], next_after: null } }));
    on("GET", /^\/runs\/rx$/, () => ({ body: run("rx", "failed") }));
    on("POST", /\/documents\/x\.md\/reingest$/, () => ({ status: 409, body: { detail: "another ingest run of the document was started concurrently" } }));
    await renderDetail();
    expect(await screen.findByTestId("knowledge-document-stuck")).toHaveTextContent("The ingest did not start.");
    fireEvent.click(screen.getByTestId("knowledge-document-retry"));
    expect(await screen.findByTestId("knowledge-documents-error")).toHaveTextContent("started concurrently");
    expect(screen.queryByTestId("knowledge-documents-info")).toBeNull();
  });

  it("pending for over a minute with no run recorded (404): Retry", async () => {
    on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => ({ body: { items: [doc("gone.md", { status: "pending", last_run_id: "rg", updated_at: "2026-01-01T00:00:00Z" })], next_after: null } }));
    on("GET", /^\/runs\/rg$/, () => ({ status: 404, body: { detail: "Run not found" } }));
    await renderDetail();
    expect(await screen.findByTestId("knowledge-document-retry")).toHaveAccessibleName("Retry ingest of gone");
  });
});

describe("/knowledge/:id", () => {
  it("header shows the name, totals and ingest cost; tabs are links; failed documents show the reason and Retry", async () => {
    on("POST", /\/documents\/billing\.md\/reingest$/, () => ({ status: 202, body: { run_id: "r2", kind: "kb_ingest", knowledge_base_id: "kb1", document: doc("billing.md", { status: "pending", last_run_id: "r2", error: null }) } }));
    await renderDetail();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Product docs");
    expect(screen.getByTestId("knowledge-ingest-cost")).toHaveTextContent("$0.0042");
    expect(screen.getByTestId("knowledge-ingest-tokens")).toHaveTextContent("4,200 tokens · 3 requests");
    expect(screen.getByTestId("knowledge-tab-search")).toHaveAttribute("href", "/knowledge/kb1?tab=search");
    expect(screen.getByTestId("knowledge-tab-runs")).toHaveAttribute("href", "/knowledge/kb1?tab=runs");
    expect(screen.getByTestId("knowledge-document-error")).toHaveTextContent("The embedding provider failed. (provider down)");
    fireEvent.click(screen.getByTestId("knowledge-document-retry"));
    await waitFor(() => expect(sent("POST", /reingest$/)).toHaveLength(1));
    await waitFor(() => expect(screen.getAllByTestId("knowledge-document-status").map((el) => el.getAttribute("data-status"))).toContain("pending"));
  });

  it("an in-flight document's progress comes from its run stream (ingest_progress)", async () => {
    on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => ({ body: { items: [doc("guide.md", { status: "ingesting", last_run_id: "run-live" })], next_after: null } }));
    const encoder = new TextEncoder();
    const fetchMock = vi.mocked(fetch);
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).includes("/runs/run-live/stream")) {
        const frame = `event: run_event\nid: 3\ndata: ${JSON.stringify({ seq: 3, type: "ingest_progress", data: { embedded: 5, total: 12 }, ts: T0 })}\n\n`;
        return new Response(new ReadableStream({ start(controller) { controller.enqueue(encoder.encode(frame)); } }), { status: 200 });
      }
      return base(input, init);
    });
    await renderDetail();
    expect(await screen.findByTestId("knowledge-document-progress")).toHaveTextContent("5 of 12 sections embedded");
  });

  it("uploads several files at once and refuses an unsupported one", async () => {
    on("PUT", /\/documents\/[^/]+$/, (url) => {
      const key = decodeURIComponent(url.pathname.split("/").pop()!);
      return key === "same.md"
        ? { status: 200, body: { unchanged: true, created: false, document: doc(key), run_id: null } }
        : { status: 202, body: { unchanged: false, created: true, document: doc(key, { status: "pending", last_run_id: `run-${key}` }), run_id: `run-${key}` } };
    });
    await renderDetail();
    fireEvent.click(screen.getByTestId("knowledge-upload"));
    const sheet = await screen.findByTestId("knowledge-upload-sheet");
    const files = [
      new File(["# Guide\n\nHello"], "guide.md", { type: "text/markdown" }),
      new File(["<h1>Page</h1>"], "page.html", { type: "text/html" }),
      new File(["same"], "same.md"),
      new File(["PK"], "manual.docx", { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" }),
    ];
    fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), { dataTransfer: { files } });
    expect(within(sheet).getByTestId("knowledge-upload-refused")).toHaveTextContent(/Unsupported file type/);
    fireEvent.change(within(sheet).getByTestId("knowledge-upload-metadata"), { target: { value: "lang: en" } });
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    await waitFor(() => expect(within(sheet).getAllByTestId("knowledge-upload-file").map((row) => row.getAttribute("data-status"))).toEqual(["started", "started", "unchanged", "refused"]));
    const puts = sent("PUT", /\/documents\//);
    expect(puts.map((r) => decodeURIComponent(r.url.pathname.split("/").pop()!))).toEqual(["guide.md", "page.html", "same.md"]);
    expect(puts[0].body).toEqual({ document_key: "guide.md", title: "guide", content: "# Guide\n\nHello", mime_type: "text/markdown", metadata: { lang: "en" } });
    expect(puts[1].body).toMatchObject({ mime_type: "text/html", content: "<h1>Page</h1>" });
    expect(screen.getAllByTestId("knowledge-document-row").length).toBeGreaterThanOrEqual(4);
  });

  it("delete lists the referencing agents; the API's 409 turns the button into Delete anyway (force)", async () => {
    let forced = false;
    on("DELETE", /^\/knowledge-bases\/kb1$/, (url) => {
      if (url.searchParams.get("force") === "true") {
        forced = true;
        return { status: 204 };
      }
      return { status: 409, body: { detail: "knowledge base is attached to 1 agent(s) (Support)", agents: [{ agent_id: "a1", agent_name: "Support", version: 3, archived: false }] } };
    });
    await renderDetail();
    fireEvent.click(screen.getByTestId("knowledge-delete"));
    const references = await screen.findByTestId("knowledge-delete-references");
    await waitFor(() => expect(references).toHaveTextContent("No agent attaches it."));
    fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
    await waitFor(() => expect(screen.getAllByTestId("knowledge-delete-reference")[0]).toHaveTextContent("Support"));
    fireEvent.click(screen.getByRole("button", { name: "Delete anyway" }));
    await waitFor(() => expect(forced).toBe(true));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/knowledge");
  });

  it("reindex shows the dry-run estimate and blocks a run over the budget", async () => {
    on("POST", /\/reindex$/, (_url, init) => {
      const body = JSON.parse(String(init.body));
      return body.dry_run
        ? { body: { dry_run: true, knowledge_base_id: "kb1", embedding_model: body.embedding_model, dimensions: 1536, chunks: 9, approx_tokens: 3_000_000, max_ingest_tokens: 2_000_000, exceeds_budget: true, estimated_cost_usd: "0.06", price_source: "override", cost_note: null, reindex_pending: false } }
        : { status: 202, body: { run_id: "rx", kind: "kb_ingest", knowledge_base_id: "kb1" } };
    });
    await renderDetail();
    fireEvent.click(screen.getByTestId("knowledge-reindex"));
    const dialog = await screen.findByTestId("knowledge-reindex-dialog");
    await waitFor(() => expect(within(dialog).getByTestId("knowledge-reindex-chunks")).toHaveTextContent("9"));
    expect(within(dialog).getByTestId("knowledge-reindex-cost")).toHaveTextContent("$0.06");
    expect(within(dialog).getByTestId("knowledge-reindex-tokens")).toHaveTextContent("3M of 2M allowed per run");
    expect(within(dialog).getByTestId("knowledge-reindex-over-budget")).toBeInTheDocument();
    expect(within(dialog).getByTestId("knowledge-reindex-start")).toBeDisabled();
    expect(sent("POST", /\/reindex$/).every((r) => (r.body as { dry_run?: boolean }).dry_run === true)).toBe(true);
  });

  it("the search tester sends query, top_k, min score and filter, and shows scores, titles and anchor URLs", async () => {
    nextRouter.reset(["/knowledge/kb1?tab=search"]);
    on("POST", /\/search$/, () => ({ body: { run_id: "s1", status: "completed", trace_id: null, result: { knowledge_base_id: "kb1", embedding_model: "test:hash", hits: [
      { n: 1, title: "Install › Setup", url: "https://docs.example.com/install#setup", ref: "https://docs.example.com/install#setup", kb_id: "kb1", document_id: "d1", document_key: "install.md", chunk_id: "c1", doc_version: 1, score: 0.87654, content: "Run the installer." },
      { n: 2, title: "Billing", url: null, ref: "kb:billing.md", kb_id: "kb1", document_id: "d2", document_key: "billing.md", chunk_id: "c2", doc_version: 1, score: 0.5, content: "Monthly." },
    ] } } }));
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const form = await screen.findByTestId("knowledge-search-form");
    fireEvent.change(within(form).getByTestId("knowledge-search-query"), { target: { value: "install" } });
    fireEvent.change(within(form).getByTestId("knowledge-search-top-k"), { target: { value: "3" } });
    fireEvent.change(within(form).getByTestId("knowledge-search-min-score"), { target: { value: "0.2" } });
    fireEvent.change(within(form).getByTestId("knowledge-search-filter"), { target: { value: '{"lang": "en"}' } });
    fireEvent.click(within(form).getByTestId("knowledge-search-submit"));
    const results = await screen.findAllByTestId("knowledge-search-result");
    expect(sent("POST", /\/search$/)[0].body).toEqual({ query: "install", top_k: 3, min_score: 0.2, filter: { lang: "en" } });
    expect(within(results[0]).getByTestId("knowledge-search-score")).toHaveTextContent("0.877");
    expect(within(results[0]).getByTestId("knowledge-search-url")).toHaveAttribute("href", "https://docs.example.com/install#setup");
    expect(within(results[1]).getByTestId("knowledge-search-document")).toHaveAttribute("href", "/knowledge/kb1?doc=billing.md");
    expect(screen.getByTestId("knowledge-search-trace")).toHaveAttribute("href", "/traces?trace=s1");
  });

  it("an invalid filter is refused before any request", async () => {
    nextRouter.reset(["/knowledge/kb1?tab=search"]);
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const form = await screen.findByTestId("knowledge-search-form");
    fireEvent.change(within(form).getByTestId("knowledge-search-query"), { target: { value: "x" } });
    fireEvent.change(within(form).getByTestId("knowledge-search-filter"), { target: { value: "[1]" } });
    fireEvent.click(within(form).getByTestId("knowledge-search-submit"));
    expect(await screen.findByTestId("knowledge-search-error")).toHaveTextContent("The filter must be a JSON object.");
    expect(sent("POST", /\/search$/)).toHaveLength(0);
  });

  it("the Ingest runs tab lists kb_ingest runs; a row opens the trace Sheet in the URL", async () => {
    nextRouter.reset(["/knowledge/kb1?tab=runs"]);
    on("GET", /^\/runs$/, (url) => {
      expect(url.searchParams.get("knowledge_base_id")).toBe("kb1");
      expect(url.searchParams.get("kind")).toBe("kb_ingest");
      return { body: { items: [
        { run_id: "run-a", kind: "kb_ingest", knowledge_base_id: "kb1", agent_id: null, version: null, status: "completed", display_status: "completed", input: JSON.stringify({ document_id: "d-install.md", doc_version: 1 }), output: null, error: null, heartbeat_at: null, created_at: T0, updated_at: "2026-11-14T10:00:03Z", trace_id: null, conversation_id: null, turn: null },
        { run_id: "run-b", kind: "kb_ingest", knowledge_base_id: "kb1", agent_id: null, version: null, status: "running", display_status: "running", input: JSON.stringify({ reindex: 2 }), output: null, error: null, heartbeat_at: null, created_at: T0, updated_at: T0, trace_id: null, conversation_id: null, turn: null },
      ], next_before: null } };
    });
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const rows = await screen.findAllByTestId("knowledge-run-row");
    await waitFor(() => expect(within(rows[0]).getByTestId("knowledge-run-mode")).toHaveTextContent("install v1"));
    expect(within(rows[1]).getByTestId("knowledge-run-mode")).toHaveTextContent("Reindex (generation 2)");
    fireEvent.click(within(rows[0]).getByRole("button", { name: /install v1/ }));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/knowledge/kb1?tab=runs&trace=run-a");
  });

  it("a document row pushes ?doc= and opens its Sheet (versions, metadata); Delete confirms and removes the row", async () => {
    on("GET", /^\/knowledge-bases\/kb1\/documents\/install\.md$/, () => ({ body: doc("install.md", { active_version: 2, latest_version: 3, status: "ingesting", metadata: { lang: "en" } }) }));
    on("DELETE", /^\/knowledge-bases\/kb1\/documents\/install\.md$/, () => ({ status: 204 }));
    await renderDetail();
    const row = screen.getAllByTestId("knowledge-document-row").find((r) => within(r).queryByText("install"))!;
    fireEvent.click(within(row).getByRole("button", { name: /install/ }));
    await act(() => nextRouter.settle());
    expect(nextRouter.entries).toEqual(["/knowledge/kb1", "/knowledge/kb1?doc=install.md"]);
    const sheet = await screen.findByTestId("knowledge-document-sheet");
    await waitFor(() => expect(within(sheet).getByTestId("knowledge-document-versions")).toHaveTextContent("Active v2 · latest v3"));
    expect(within(sheet).getByTestId("knowledge-document-metadata")).toHaveTextContent("lang: en");
    fireEvent.click(within(sheet).getByTestId("knowledge-document-delete"));
    fireEvent.click(await screen.findByRole("button", { name: "Delete document" }));
    await waitFor(() => expect(sent("DELETE", /\/documents\/install\.md$/)).toHaveLength(1));
    await waitFor(() => expect(screen.getAllByTestId("knowledge-document-row")).toHaveLength(1));
  });

  it("a file that is not valid UTF-8 is refused client-side with a clear message, and nothing is sent", async () => {
    await renderDetail();
    fireEvent.click(screen.getByTestId("knowledge-upload"));
    const sheet = await screen.findByTestId("knowledge-upload-sheet");
    fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), { dataTransfer: { files: [new File([new Uint8Array([0x23, 0x20, 0xff, 0xfe])], "latin.md")] } });
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    const row = await within(sheet).findByText(/not valid UTF-8/);
    const item = row.closest("[data-testid='knowledge-upload-file']") as HTMLElement;
    // Refused (it cannot be sent as it is), not failed: never re-sent, and it can be removed.
    expect(item).toHaveAttribute("data-status", "refused");
    expect(within(item).getByText("Can't read")).toBeInTheDocument();
    expect(within(sheet).getByTestId("knowledge-upload-submit")).toBeDisabled();
    expect(within(sheet).getByTestId("knowledge-upload-submit")).toHaveTextContent("Upload files");
    fireEvent.click(within(item).getByRole("button", { name: "Remove latin.md" }));
    expect(within(sheet).queryByTestId("knowledge-upload-file")).toBeNull();
    expect(sent("PUT", /\/documents\//)).toHaveLength(0);
  });

  it("a binary file under a text name is refused client-side with what it looks like, as the API says it", async () => {
    await renderDetail();
    fireEvent.click(screen.getByTestId("knowledge-upload"));
    const sheet = await screen.findByTestId("knowledge-upload-sheet");
    const pdf = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x37, 0x0a, 0xe2, 0xe3, 0xcf, 0xd3]);
    const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00]);
    fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), {
      dataTransfer: { files: [new File([pdf], "report.txt"), new File([png], "logo.yaml")] },
    });
    expect(within(sheet).getByTestId("knowledge-upload-submit")).toHaveTextContent("Upload 2 files");
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    expect(await within(sheet).findByText("The file's content does not match its declared type. It looks like application/pdf, not text/plain.")).toBeInTheDocument();
    expect(await within(sheet).findByText("The file's content does not match its declared type. It looks like image/png, not application/yaml.")).toBeInTheDocument();
    const items = within(sheet).getAllByTestId("knowledge-upload-file");
    expect(items.map((i) => i.getAttribute("data-status"))).toEqual(["refused", "refused"]);
    expect(within(sheet).getByTestId("knowledge-upload-submit")).toBeDisabled();
    expect(sent("PUT", /\/documents\//)).toHaveLength(0);
  });

  it("an oversize file's chip says Too large, an unsupported one's Not supported", async () => {
    await renderDetail();
    fireEvent.click(screen.getByTestId("knowledge-upload"));
    const sheet = await screen.findByTestId("knowledge-upload-sheet");
    const big = new File(["x"], "big.yaml");
    Object.defineProperty(big, "size", { value: 3 * 1024 * 1024 });
    fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), {
      dataTransfer: { files: [big, new File(["x"], "a.docx")] },
    });
    const [tooBig, unsupported] = within(sheet).getAllByTestId("knowledge-upload-file");
    expect(within(tooBig).getByText("Too large")).toBeInTheDocument();
    expect(within(tooBig).getByText("The file is larger than 2 MiB.")).toBeInTheDocument();
    expect(within(tooBig).queryByText("Not supported")).toBeNull();
    expect(within(unsupported).getByText("Not supported")).toBeInTheDocument();
  });

  it("a pasted document whose JSON body is over the cap is refused before any request", async () => {
    await renderDetail();
    fireEvent.click(screen.getByTestId("knowledge-upload"));
    const sheet = await screen.findByTestId("knowledge-upload-sheet");
    fireEvent.mouseDown(within(sheet).getByTestId("knowledge-upload-tab-paste"));
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-tab-paste"));
    const paste = await within(sheet).findByTestId("knowledge-upload-paste");
    fireEvent.change(within(paste).getByTestId("knowledge-paste-key"), { target: { value: "big.md" } });
    // 3 MiB of quotes: 3 MiB of content, 6 MiB once JSON-escaped.
    fireEvent.change(within(paste).getByTestId("knowledge-paste-content"), { target: { value: '"'.repeat(3 * 1024 * 1024) } });
    fireEvent.click(within(paste).getByTestId("knowledge-paste-submit"));
    expect(await within(paste).findByTestId("knowledge-paste-result")).toHaveTextContent("The upload is larger than 5 MiB once encoded (JSON).");
    expect(sent("PUT", /\/documents\//)).toHaveLength(0);
  });

  it("leaving the page aborts the upload in flight and sends nothing after it", async () => {
    const signals: AbortSignal[] = [];
    const fetchMock = vi.mocked(fetch);
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if ((init?.method ?? "GET") === "PUT") {
        signals.push(init!.signal!);
        requests.push({ method: "PUT", url: new URL(String(input)), body: undefined });
        return new Promise<Response>((_resolve, reject) => init!.signal!.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError"))));
      }
      return base(input, init);
    });
    const view = render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-document-row");
    fireEvent.click(screen.getByTestId("knowledge-upload"));
    const sheet = await screen.findByTestId("knowledge-upload-sheet");
    fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), { dataTransfer: { files: [new File(["a"], "a.md"), new File(["b"], "b.md")] } });
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    await waitFor(() => expect(signals).toHaveLength(1));
    view.unmount();
    expect(signals[0].aborted).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(sent("PUT", /\/documents\//)).toHaveLength(1);
  });

  it("leaving the Search tab aborts the search and stops polling its run", async () => {
    nextRouter.reset(["/knowledge/kb1?tab=search"]);
    let searchSignal: AbortSignal | null = null;
    const fetchMock = vi.mocked(fetch);
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/search")) {
        searchSignal = init!.signal!;
        return new Response(JSON.stringify({ run_id: "s1", status: "running" }), { status: 202, headers: { "content-type": "application/json" } });
      }
      return base(input, init);
    });
    const view = render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const form = await screen.findByTestId("knowledge-search-form");
    fireEvent.change(within(form).getByTestId("knowledge-search-query"), { target: { value: "x" } });
    fireEvent.click(within(form).getByTestId("knowledge-search-submit"));
    await waitFor(() => expect(searchSignal).not.toBeNull());
    view.unmount();
    expect(searchSignal!.aborted).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 1_100));
    expect(sent("GET", /^\/runs\/s1$/)).toHaveLength(0);
  });

  it("the in-flight poll skips a tick while one is pending and backs off 3 s → 6 s on errors", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => ({ body: { items: [doc("slow.md", { status: "ingesting", last_run_id: null })], next_after: null } }));
      let release: (() => void) | null = null;
      let mode: "hang" | "fail" = "hang";
      const fetchMock = vi.mocked(fetch);
      const base = fetchMock.getMockImplementation()!;
      fetchMock.mockImplementation(async (input, init) => {
        if (String(input).endsWith("/documents/slow.md")) {
          requests.push({ method: "GET", url: new URL(String(input)), body: undefined });
          if (mode === "hang") {
            await new Promise<void>((resolve) => { release = resolve; });
          }
          return new Response(JSON.stringify({ detail: "down" }), { status: 500, headers: { "content-type": "application/json" } });
        }
        return base(input, init);
      });
      await renderDetail();
      const polls = () => sent("GET", /\/documents\/slow\.md$/).length;
      await act(async () => { vi.advanceTimersByTime(3_000); });
      expect(polls()).toBe(1);
      // Two more ticks while the first read hangs: skipped.
      await act(async () => { vi.advanceTimersByTime(6_000); });
      expect(polls()).toBe(1);
      mode = "fail";
      await act(async () => { release!(); await Promise.resolve(); });
      // That read failed: the next one waits 6 s, not 3 s.
      await act(async () => { vi.advanceTimersByTime(3_000); });
      expect(polls()).toBe(1);
      await act(async () => { vi.advanceTimersByTime(3_000); });
      await waitFor(() => expect(polls()).toBe(2));
    } finally {
      vi.useRealTimers();
    }
  });

  it("a poll read that hangs is abandoned after 10 s (aborted), so the next tick runs", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"], shouldAdvanceTime: true });
    try {
      on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => ({ body: { items: [doc("stuck.md", { status: "ingesting", last_run_id: null })], next_after: null } }));
      const signals: AbortSignal[] = [];
      const fetchMock = vi.mocked(fetch);
      const base = fetchMock.getMockImplementation()!;
      fetchMock.mockImplementation(async (input, init) => {
        if (String(input).endsWith("/documents/stuck.md")) {
          requests.push({ method: "GET", url: new URL(String(input)), body: undefined });
          signals.push(init!.signal!);
          // Never answers; only an abort ends it.
          return new Promise<Response>((_resolve, reject) => init!.signal!.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError"))));
        }
        return base(input, init);
      });
      await renderDetail();
      const polls = () => sent("GET", /\/documents\/stuck\.md$/).length;
      await act(async () => { vi.advanceTimersByTime(3_000); });
      expect(polls()).toBe(1);
      await act(async () => { vi.advanceTimersByTime(9_000); });
      expect(polls()).toBe(1);
      expect(signals[0].aborted).toBe(false);
      await act(async () => { vi.advanceTimersByTime(1_000); });
      expect(signals[0].aborted).toBe(true);
      // The timeout counts as a failure: the next read comes after the 6 s backoff, not never.
      await act(async () => { vi.advanceTimersByTime(6_000); });
      await waitFor(() => expect(polls()).toBe(2));
    } finally {
      vi.useRealTimers();
    }
  });

  it("an unknown knowledge base shows Not found", async () => {
    params.id = "nope";
    on("GET", /^\/knowledge-bases\/nope$/, () => ({ status: 404, body: { detail: "Knowledge base not found" } }));
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    expect(await screen.findByTestId("knowledge-not-found")).toBeInTheDocument();
  });
});

describe("/knowledge/:id sheets (milestone browser check, item 3)", () => {
  it("the document Sheet opens with focus on its title, never on Delete", async () => {
    on("GET", /^\/knowledge-bases\/kb1\/documents\/install\.md$/, () => ({ body: doc("install.md") }));
    await renderDetail();
    const row = screen.getAllByTestId("knowledge-document-row").find((r) => within(r).queryByText("install"))!;
    fireEvent.click(within(row).getByRole("button", { name: /install/ }));
    await act(() => nextRouter.settle());
    const sheet = await screen.findByTestId("knowledge-document-sheet");
    await waitFor(() => expect(within(sheet).getByTestId("knowledge-document-title")).toHaveFocus());
    expect(within(sheet).getByTestId("knowledge-document-delete")).not.toHaveFocus();
    expect(within(sheet).getByTestId("knowledge-document-title")).toHaveTextContent("install");
  });

  it("closing the upload Sheet clears its files, metadata and paste form; reopening starts empty", async () => {
    on("PUT", /\/documents\/[^/]+$/, (url) => {
      const key = decodeURIComponent(url.pathname.split("/").pop()!);
      return { status: 202, body: { unchanged: false, created: true, document: doc(key, { status: "pending" }), run_id: `run-${key}` } };
    });
    await renderDetail();
    fireEvent.click(screen.getByTestId("knowledge-upload"));
    let sheet = await screen.findByTestId("knowledge-upload-sheet");
    fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), {
      dataTransfer: { files: [new File(["# Guide"], "guide.md", { type: "text/markdown" }), new File(["PK"], "x.docx", { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" })] },
    });
    fireEvent.change(within(sheet).getByTestId("knowledge-upload-metadata"), { target: { value: "lang: en" } });
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    await waitFor(() => expect(within(sheet).getAllByTestId("knowledge-upload-file").map((r) => r.getAttribute("data-status"))).toEqual(["started", "refused"]));
    fireEvent.mouseDown(within(sheet).getByTestId("knowledge-upload-tab-paste"));
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-tab-paste"));
    fireEvent.change(await within(sheet).findByTestId("knowledge-paste-title"), { target: { value: "FAQ" } });

    const footerClose = within(sheet).getAllByRole("button", { name: "Close" }).find((b) => b.textContent === "Close")!;
    fireEvent.click(footerClose);
    await waitFor(() => expect(screen.queryByTestId("knowledge-upload-sheet")).toBeNull());

    fireEvent.click(screen.getByTestId("knowledge-upload"));
    sheet = await screen.findByTestId("knowledge-upload-sheet");
    expect(within(sheet).queryAllByTestId("knowledge-upload-file")).toHaveLength(0);
    expect(within(sheet).getByTestId("knowledge-upload-metadata")).toHaveValue("");
    expect(within(sheet).getByTestId("knowledge-upload-submit")).toBeDisabled();
    fireEvent.mouseDown(within(sheet).getByTestId("knowledge-upload-tab-paste"));
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-tab-paste"));
    expect(await within(sheet).findByTestId("knowledge-paste-title")).toHaveValue("");
  });
});
