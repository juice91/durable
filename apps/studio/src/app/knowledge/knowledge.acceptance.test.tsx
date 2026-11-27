/**
 * RM.16 acceptance: what knowledge.test.tsx does not cover. Creating with the model picker, the list's
 * partial failures, upload caps and per-file status, paste, live progress (3-stream cap, 3 s polling
 * for the rest), retry, the document Sheet's deep link, versions and delete, reindex (estimate,
 * over budget, start, banner, cancel), deleting a knowledge base (agent scan plus the API's 409),
 * the search tester (validation, scores, URLs, trace link, 30 s polling cap), the ingest runs tab,
 * citations in chat, kb_ingest's "Open knowledge base" link, navigation (`g k`, `c`, palette), the
 * phone layout and axe in both themes. The API is an in-memory `fetch` stub; the router is the shared
 * Next router mock.
 */
import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const params = vi.hoisted(() => ({ id: "kb1" }));
vi.mock("next/navigation", async () => {
  const mod = (await import("@/test/nextRouterMock")).navigationModule;
  return { ...mod, useParams: () => params };
});
// MarkdownText reads its message id from assistant-ui; every render is the turn "run-1".
vi.mock("@assistant-ui/react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@assistant-ui/react")>()),
  useAuiState: (selector: (state: { message: { id: string } }) => unknown) => selector({ message: { id: "run-1:assistant-1" } }),
}));

import { nextRouter } from "@/test/nextRouterMock";
import { AXE_TIMEOUT_MS, expectNoAxeViolations, PAGE_RULES } from "@/test/a11y";
import { KeyboardHarness, pressKey, pressKeys } from "@/components/shortcuts/shortcutsTesting";
import { MarkdownText, ToolCallInfoContext } from "@/components/chat/messageParts";
import type { CitationSource } from "@/components/chat/citations";
import { toolCallInfoKey, toolCallInfos, type ToolCallInfo } from "@/components/chat/toolCalls";
import type { RunEvent } from "@/lib/runs";
import { mockMatchMedia, type MatchMediaControl } from "@/components/frame/frameTesting";
import { THEME_STORAGE_KEY } from "@/lib/theme";
import { allNavItems } from "@/lib/nav";
import { checkUploadFile, DEFAULT_MAX_DOCUMENT_BYTES, documentPutBytes, documentPutProblem, KnowledgeBaseInUseError, deleteKnowledgeBase } from "@/lib/knowledge";
import { resetPaletteCache } from "@/components/palette/paletteData";
import KnowledgePage from "./page";
import KnowledgeBasePage from "./[id]/page";
import { optionsOf } from "@/test/select";

const T0 = "2026-11-14T10:00:00Z";

function kb(extra: Record<string, unknown> = {}) {
  return {
    id: "kb1", tenant_id: "t1", name: "Product docs", description: "Manuals", embedding_model: "test:hash", dimensions: 1536,
    chunking: {}, active_generation: 1, last_generation: 1, pending_generation: null, pending_embedding_model: null, pending_dimensions: null,
    status: "ready", error: null, created_at: T0, updated_at: T0, document_count: 2, chunk_count: 9, ...extra,
  };
}

function detail(extra: Record<string, unknown> = {}) {
  return { ...kb(), ingest_tokens: 4200, ingest_cost_usd: "0.0042", ingest_requests: 3, ...extra };
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
/** Run id → its open stream (what the page follows); `push` sends an SSE frame, `end` closes it. */
let streams: Map<string, { push: (event: Record<string, unknown>) => void; end: () => void }>;

function on(method: string, path: RegExp, handler: Handler) {
  routesTable.unshift([path, method, handler]);
}

const apiPath = (url: URL) => url.pathname.replace(/^\/api\/v1/, "");
const sent = (method: string, path: RegExp) => requests.filter((r) => r.method === method && path.test(apiPath(r.url)));
const json = (status: number, body: unknown) => ({ status, body });

function openStream(runId: string): Response {
  const encoder = new TextEncoder();
  let seq = 0;
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start(c) { controller = c; } });
  streams.set(runId, {
    push: (event) => {
      seq += 1;
      controller.enqueue(encoder.encode(`event: run_event\nid: ${seq}\ndata: ${JSON.stringify({ seq, ts: T0, ...event })}\n\n`));
    },
    end: () => { try { controller.close(); } catch { /* already closed */ } },
  });
  return new Response(body, { status: 200 });
}

beforeEach(() => {
  nextRouter.reset(["/knowledge/kb1"]);
  params.id = "kb1";
  requests = [];
  routesTable = [];
  streams = new Map();
  resetPaletteCache();
  on("GET", /^\/knowledge-bases$/, () => json(200, { items: [kb(), kb({ id: "kb2", name: "Handbook", document_count: 0, chunk_count: 0 })] }));
  on("GET", /^\/knowledge-bases\/kb1$/, () => json(200, detail()));
  on("GET", /^\/knowledge-bases\/kb2$/, () => json(200, detail({ id: "kb2", name: "Handbook", ingest_cost_usd: null })));
  on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => json(200, { items: [doc("install.md"), doc("billing.md")], next_after: null }));
  on("GET", /^\/embedding-models$/, () => json(200, MODELS));
  on("GET", /^\/agents$/, () => json(200, []));
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input);
      const path = apiPath(url);
      const method = (init.method ?? "GET").toUpperCase();
      requests.push({ method, url, body: init.body ? JSON.parse(String(init.body)) : undefined });
      const stream = /^\/runs\/([^/]+)\/stream$/.exec(path);
      if (stream) return openStream(decodeURIComponent(stream[1]));
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
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  document.documentElement.classList.remove("dark");
});

async function renderDetail(tab?: "search" | "runs") {
  if (tab) nextRouter.reset([`/knowledge/kb1?tab=${tab}`]);
  render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
  if (!tab) await screen.findAllByTestId("knowledge-document-row");
}

async function openUpload() {
  await renderDetail();
  fireEvent.click(screen.getByTestId("knowledge-upload"));
  return screen.findByTestId("knowledge-upload-sheet");
}

const putKey = (r: { url: URL }) => decodeURIComponent(r.url.pathname.split("/").pop()!);
const accepted = (key: string) => json(202, { unchanged: false, created: true, document: doc(key, { status: "pending", last_run_id: `run-${key}` }), run_id: `run-${key}` });

describe("knowledge bases: the list", () => {
  it("a detail that fails leaves that row's cost as a dash; the others still show theirs", async () => {
    on("GET", /^\/knowledge-bases\/kb2$/, () => json(500, { detail: "boom" }));
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    const rows = await screen.findAllByTestId("knowledge-base-row");
    const product = rows.find((row) => within(row).queryByText("Product docs"))!;
    const handbook = rows.find((row) => within(row).queryByText("Handbook"))!;
    await waitFor(() => expect(within(product).getByTestId("knowledge-base-cost")).toHaveTextContent("$0.0042"));
    expect(within(handbook).getByTestId("knowledge-base-cost")).toHaveTextContent("—");
    expect(screen.queryByTestId("datalist-error")).toBeNull();
  });

  it("a failed list shows the error; Retry loads it again", async () => {
    let fail = true;
    on("GET", /^\/knowledge-bases$/, () => (fail ? json(503, { detail: "API down" }) : json(200, { items: [kb()] })));
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    const error = await screen.findByTestId("datalist-error");
    expect(error).toHaveTextContent("API down");
    fail = false;
    fireEvent.click(within(error).getByRole("button", { name: "Retry" }));
    expect(await screen.findAllByTestId("knowledge-base-row")).toHaveLength(1);
    expect(screen.queryByTestId("datalist-error")).toBeNull();
  });

  it("shows sections, a reindexing status and a null cost as 'No price'", async () => {
    on("GET", /^\/knowledge-bases$/, () => json(200, { items: [kb({ status: "reindexing", chunk_count: 1234 })] }));
    on("GET", /^\/knowledge-bases\/kb1$/, () => json(200, detail({ ingest_cost_usd: null })));
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    const row = await screen.findByTestId("knowledge-base-row");
    expect(within(row).getByTestId("knowledge-base-status")).toHaveAttribute("data-status", "reindexing");
    expect(within(row).getByTestId("knowledge-base-sections")).toHaveTextContent("1234");
    await waitFor(() => expect(within(row).getByTestId("knowledge-base-cost")).toHaveTextContent("No price"));
  });
});

describe("knowledge bases: create with the model picker", () => {
  async function openCreate() {
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-base-row");
    fireEvent.click(screen.getByTestId("knowledge-new"));
    const dialog = await screen.findByTestId("knowledge-create-dialog");
    const picker = within(dialog).getByTestId("knowledge-embedding-model") as HTMLSelectElement;
    await waitFor(() => expect(picker).not.toBeDisabled());
    return { dialog, picker };
  }

  it("offers exactly the allowlisted models: the concrete one with its dims, the wildcard as 'any model'", async () => {
    const { picker } = await openCreate();
    expect(optionsOf(picker).map((o) => [o.value, o.textContent])).toEqual([
      ["", "Choose a model"],
      ["test:hash", "test:hash (1536 dims)"],
      ["litellm:*", "litellm: any model (1024 dims)"],
    ]);
  });

  it("a concrete model creates without a name field; the description is sent trimmed", async () => {
    on("POST", /^\/knowledge-bases$/, () => json(201, kb({ id: "kb7", name: "Docs" })));
    const { dialog, picker } = await openCreate();
    fireEvent.change(within(dialog).getByTestId("knowledge-create-name"), { target: { value: "Docs" } });
    fireEvent.change(within(dialog).getByTestId("knowledge-create-description"), { target: { value: "  What it covers  " } });
    fireEvent.change(picker, { target: { value: "test:hash" } });
    expect(within(dialog).queryByTestId("knowledge-embedding-model-name")).toBeNull();
    fireEvent.click(within(dialog).getByTestId("knowledge-create-submit"));
    await waitFor(() => expect(sent("POST", /^\/knowledge-bases$/)).toHaveLength(1));
    expect(sent("POST", /^\/knowledge-bases$/)[0].body).toEqual({ name: "Docs", description: "What it covers", embedding_model: "test:hash" });
  });

  it("a wildcard model needs a name: blank or spaces keeps Create disabled, then provider:name is sent", async () => {
    on("POST", /^\/knowledge-bases$/, () => json(201, kb({ id: "kb8" })));
    const { dialog, picker } = await openCreate();
    const submit = within(dialog).getByTestId("knowledge-create-submit");
    fireEvent.change(within(dialog).getByTestId("knowledge-create-name"), { target: { value: "Wild" } });
    fireEvent.change(picker, { target: { value: "litellm:*" } });
    expect(submit).toBeDisabled();
    fireEvent.change(within(dialog).getByTestId("knowledge-embedding-model-name"), { target: { value: "   " } });
    expect(submit).toBeDisabled();
    fireEvent.change(within(dialog).getByTestId("knowledge-embedding-model-name"), { target: { value: " qwen3-embed " } });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await waitFor(() => expect(sent("POST", /^\/knowledge-bases$/)[0]?.body).toMatchObject({ embedding_model: "litellm:qwen3-embed" }));
  });

  it("an API refusal (name taken, 409) stays in the dialog with the message and nothing navigates", async () => {
    on("POST", /^\/knowledge-bases$/, () => json(409, { detail: "a knowledge base named 'Docs' exists" }));
    const { dialog, picker } = await openCreate();
    fireEvent.change(within(dialog).getByTestId("knowledge-create-name"), { target: { value: "Docs" } });
    fireEvent.change(picker, { target: { value: "test:hash" } });
    fireEvent.click(within(dialog).getByTestId("knowledge-create-submit"));
    expect(await within(dialog).findByTestId("knowledge-create-error")).toHaveTextContent("a knowledge base named 'Docs' exists");
    expect(within(dialog).getByTestId("knowledge-create-submit")).toBeEnabled();
    expect(nextRouter.url).toBe("/knowledge/kb1");
  });

  it("an empty allowlist disables the picker and says how to allow one", async () => {
    on("GET", /^\/embedding-models$/, () => json(200, { items: [] }));
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-base-row");
    fireEvent.click(screen.getByTestId("knowledge-new"));
    const dialog = await screen.findByTestId("knowledge-create-dialog");
    await waitFor(() => expect(within(dialog).getByText(/EAP_ALLOWED_EMBEDDING_MODELS/)).toBeInTheDocument());
    expect(within(dialog).getByTestId("knowledge-embedding-model")).toBeDisabled();
    expect(within(dialog).getByTestId("knowledge-create-submit")).toBeDisabled();
  });

  it("a models load failure shows the error with Retry, which loads them", async () => {
    let fail = true;
    on("GET", /^\/embedding-models$/, () => (fail ? json(500, { detail: "models down" }) : json(200, MODELS)));
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-base-row");
    fireEvent.click(screen.getByTestId("knowledge-new"));
    const dialog = await screen.findByTestId("knowledge-create-dialog");
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("models down");
    fail = false;
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(within(dialog).getByTestId("knowledge-embedding-model")).not.toBeDisabled());
  });
});

function fileOf(name: string, content = "hello", size?: number): File {
  const file = new File([content], name);
  if (size !== undefined) Object.defineProperty(file, "size", { value: size });
  return file;
}

const statuses = (sheet: HTMLElement) => within(sheet).getAllByTestId("knowledge-upload-file").map((row) => row.getAttribute("data-status"));
const drop = (sheet: HTMLElement, files: File[]) => fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), { dataTransfer: { files } });

describe("upload: size and type refusals follow the API's caps", () => {
  it("the cap is the API's EAP_KB_MAX_DOCUMENT_BYTES default (5 MiB) on the encoded JSON body: a body of exactly the limit is accepted, one byte over is refused", () => {
    expect(DEFAULT_MAX_DOCUMENT_BYTES).toBe(5 * 1024 * 1024);
    const key = "a.md";
    const bodyOf = (content: string) => ({ content, mime_type: "text/markdown" });
    const overhead = documentPutBytes(key, bodyOf(""));
    const exact = bodyOf("x".repeat(DEFAULT_MAX_DOCUMENT_BYTES - overhead));
    expect(documentPutBytes(key, exact)).toBe(DEFAULT_MAX_DOCUMENT_BYTES);
    expect(documentPutProblem(key, exact)).toBeNull();
    expect(documentPutProblem(key, bodyOf("x".repeat(DEFAULT_MAX_DOCUMENT_BYTES - overhead + 1)))).toBe("The upload is larger than 5 MiB once encoded (JSON).");
    // Escaping and multi-byte text count as sent: 1 MiB of quotes is 2 MiB of JSON, "é" is 2 bytes.
    expect(documentPutBytes(key, bodyOf('"'.repeat(1024)))).toBe(overhead + 2048);
    expect(documentPutBytes(key, bodyOf("é"))).toBe(overhead + 2);
    // The file-size test is only a pre-check: content alone over the cap can never fit.
    expect(checkUploadFile({ name: "a.md", size: DEFAULT_MAX_DOCUMENT_BYTES }).ok).toBe(true);
    expect(checkUploadFile({ name: "a.md", size: DEFAULT_MAX_DOCUMENT_BYTES + 1 })).toEqual({ ok: false, code: "too_large", reason: "The file is larger than 5 MiB." });
  });

  it("accepts .md .markdown .txt .text .html .htm .pdf in any case (their MIME types); refuses the rest by name", async () => {
    on("PUT", /\/documents\/[^/]+$/, (url) => accepted(decodeURIComponent(url.pathname.split("/").pop()!)));
    const sheet = await openUpload();
    drop(sheet, [fileOf("A.MD"), fileOf("b.markdown"), fileOf("c.txt"), fileOf("d.text"), fileOf("e.HTML"), fileOf("f.htm"), fileOf("g.pdf", "%PDF-1.7 hello"), fileOf("h.docx"), fileOf("noext"), fileOf("i.png")]);
    expect(statuses(sheet)).toEqual(["ready", "ready", "ready", "ready", "ready", "ready", "ready", "refused", "refused", "refused"]);
    expect(within(sheet).getAllByTestId("knowledge-upload-refused")).toHaveLength(3);
    expect(within(sheet).getByTestId("knowledge-upload-submit")).toHaveTextContent("Upload 7 files");
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    await waitFor(() => expect(sent("PUT", /\/documents\//)).toHaveLength(7), { timeout: 5000 });
    await waitFor(() => expect(statuses(sheet).slice(0, 7).every((status) => status === "started")).toBe(true), { timeout: 5000 });
    const mimes = Object.fromEntries(sent("PUT", /\/documents\//).map((r) => [putKey(r), (r.body as { mime_type: string }).mime_type]));
    expect(mimes).toEqual({ "A.MD": "text/markdown", "b.markdown": "text/markdown", "c.txt": "text/plain", "d.text": "text/plain", "e.HTML": "text/html", "f.htm": "text/html", "g.pdf": "application/pdf" });
  });

  it("an oversized file and an empty file are refused with the reason, before any request", async () => {
    const sheet = await openUpload();
    drop(sheet, [fileOf("huge.md", "x", DEFAULT_MAX_DOCUMENT_BYTES + 1), fileOf("empty.txt", "")]);
    const refused = within(sheet).getAllByTestId("knowledge-upload-refused").map((el) => el.textContent);
    expect(refused).toEqual(["The file is larger than 5 MiB.", "The file is empty."]);
    expect(within(sheet).getByTestId("knowledge-upload-submit")).toBeDisabled();
    expect(sent("PUT", /\/documents\//)).toHaveLength(0);
  });

  it("an unsupported type is refused with the supported types named", async () => {
    const sheet = await openUpload();
    drop(sheet, [fileOf("manual.docx", "PK")]);
    expect(within(sheet).getByTestId("knowledge-upload-refused")).toHaveTextContent("Unsupported file type: upload .md, .txt, .html, .rst, .pdf, .yaml, .yml or .json files.");
  });

  it("the file picker adds files the same way, and Remove drops one that has not been sent", async () => {
    const sheet = await openUpload();
    fireEvent.change(within(sheet).getByTestId("knowledge-upload-input"), { target: { files: [fileOf("one.md"), fileOf("two.md")] } });
    expect(statuses(sheet)).toEqual(["ready", "ready"]);
    fireEvent.click(within(sheet).getByRole("button", { name: "Remove one.md" }));
    expect(statuses(sheet)).toEqual(["ready"]);
    expect(within(sheet).getByTestId("knowledge-upload-submit")).toHaveTextContent("Upload 1 file");
  });

  it("invalid metadata stops the upload before any request, naming the line", async () => {
    const sheet = await openUpload();
    drop(sheet, [fileOf("a.md")]);
    fireEvent.change(within(sheet).getByTestId("knowledge-upload-metadata"), { target: { value: "lang: en\nnot a pair" } });
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent('"not a pair" is not key: value.');
    expect(sent("PUT", /\/documents\//)).toHaveLength(0);
    expect(statuses(sheet)).toEqual(["ready"]);
  });
});

describe("upload: per-file status, errors and retry", () => {
  it("each file shows uploading, then started, unchanged or failed with the API's reason; one failure does not stop the rest", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    on("PUT", /\/documents\/[^/]+$/, (url) => {
      const key = decodeURIComponent(url.pathname.split("/").pop()!);
      if (key === "bad.md") return json(413, { detail: "document is larger than EAP_KB_MAX_DOCUMENT_BYTES" });
      if (key === "dup.md") return json(200, { unchanged: true, created: false, document: doc(key), run_id: null });
      return accepted(key);
    });
    const fetchMock = vi.mocked(fetch);
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/documents/first.md") && init?.method === "PUT") await gate;
      return base(input, init);
    });
    const sheet = await openUpload();
    drop(sheet, [fileOf("first.md"), fileOf("bad.md"), fileOf("dup.md"), fileOf("last.md")]);
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    await waitFor(() => expect(statuses(sheet)[0]).toBe("uploading"));
    expect(within(sheet).getByTestId("knowledge-upload-submit")).toBeDisabled();
    release();
    await waitFor(() => expect(statuses(sheet)).toEqual(["started", "failed", "unchanged", "started"]));
    const rows = within(sheet).getAllByTestId("knowledge-upload-file");
    expect(rows[1]).toHaveTextContent("document is larger than EAP_KB_MAX_DOCUMENT_BYTES");
    expect(rows[2]).toHaveTextContent("Same content as the stored version");
    expect(sent("PUT", /\/documents\//)).toHaveLength(4);
  });

  it("an API rejection (415) is a per-file failure; fixing it and uploading again sends only the failed file", async () => {
    let healthy = false;
    on("PUT", /\/documents\/[^/]+$/, (url) => {
      const key = decodeURIComponent(url.pathname.split("/").pop()!);
      return key === "flaky.md" && !healthy ? json(415, { detail: "the content is not UTF-8 text" }) : accepted(key);
    });
    const sheet = await openUpload();
    drop(sheet, [fileOf("ok.md"), fileOf("flaky.md")]);
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    await waitFor(() => expect(statuses(sheet)).toEqual(["started", "failed"]));
    expect(within(sheet).getByTestId("knowledge-upload-submit")).toHaveTextContent("Upload 1 file");
    healthy = true;
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    await waitFor(() => expect(statuses(sheet)).toEqual(["started", "started"]));
    expect(sent("PUT", /\/documents\//).map(putKey)).toEqual(["ok.md", "flaky.md", "flaky.md"]);
  });

  it("uploaded documents appear in the Documents tab as pending rows behind the Sheet", async () => {
    on("PUT", /\/documents\/[^/]+$/, (url) => accepted(decodeURIComponent(url.pathname.split("/").pop()!)));
    const sheet = await openUpload();
    drop(sheet, [fileOf("fresh.md")]);
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    await waitFor(() => expect(statuses(sheet)).toEqual(["started"]));
    const statusesOnPage = screen.getAllByTestId("knowledge-document-status").map((el) => el.getAttribute("data-status"));
    expect(statusesOnPage).toContain("pending");
    expect(screen.getAllByTestId("knowledge-document-row")).toHaveLength(3);
  });
});

describe("upload: paste text", () => {
  async function openPaste() {
    const sheet = await openUpload();
    fireEvent.mouseDown(within(sheet).getByTestId("knowledge-upload-tab-paste"));
    return within(await screen.findByTestId("knowledge-upload-paste"));
  }

  it("sends the text with the key (from the title when none is typed), format, source URL and metadata", async () => {
    on("PUT", /\/documents\/[^/]+$/, (url) => accepted(decodeURIComponent(url.pathname.split("/").pop()!)));
    const paste = await openPaste();
    fireEvent.change(paste.getByTestId("knowledge-paste-title"), { target: { value: "Refund FAQ" } });
    fireEvent.change(paste.getByTestId("knowledge-paste-format"), { target: { value: "text/plain" } });
    fireEvent.change(paste.getByTestId("knowledge-paste-url"), { target: { value: "https://docs.example.com/refunds" } });
    fireEvent.change(paste.getByTestId("knowledge-paste-content"), { target: { value: "Refunds take 5 days." } });
    fireEvent.click(paste.getByTestId("knowledge-paste-submit"));
    expect(await paste.findByTestId("knowledge-paste-result")).toHaveTextContent("Refund FAQ: stored, ingest started.");
    expect(sent("PUT", /\/documents\//)[0].body).toEqual({
      document_key: "Refund FAQ", title: "Refund FAQ", content: "Refunds take 5 days.", mime_type: "text/plain",
      source_url: "https://docs.example.com/refunds", metadata: null,
    });
    expect(paste.getByTestId("knowledge-paste-content")).toHaveValue("");
  });

  it("refuses an empty text, a missing key and an invalid key before any request", async () => {
    const paste = await openPaste();
    fireEvent.click(paste.getByTestId("knowledge-paste-submit"));
    expect(await paste.findByTestId("knowledge-paste-result")).toHaveTextContent("Give the document a key or a title.");
    fireEvent.change(paste.getByTestId("knowledge-paste-key"), { target: { value: "a/b.md" } });
    fireEvent.click(paste.getByTestId("knowledge-paste-submit"));
    expect(paste.getByTestId("knowledge-paste-result")).toHaveTextContent("The key must not contain / or \\");
    fireEvent.change(paste.getByTestId("knowledge-paste-key"), { target: { value: "faq.md" } });
    fireEvent.click(paste.getByTestId("knowledge-paste-submit"));
    expect(paste.getByTestId("knowledge-paste-result")).toHaveTextContent("Paste some text first.");
    expect(sent("PUT", /\/documents\//)).toHaveLength(0);
  });

  it("unchanged content says so and keeps the text; an API error shows its message", async () => {
    let unchanged = true;
    on("PUT", /\/documents\/[^/]+$/, () => (unchanged ? json(200, { unchanged: true, created: false, document: doc("faq.md"), run_id: null }) : json(422, { detail: "unsupported content" })));
    const paste = await openPaste();
    fireEvent.change(paste.getByTestId("knowledge-paste-key"), { target: { value: "faq.md" } });
    fireEvent.change(paste.getByTestId("knowledge-paste-content"), { target: { value: "same" } });
    fireEvent.click(paste.getByTestId("knowledge-paste-submit"));
    expect(await paste.findByTestId("knowledge-paste-result")).toHaveTextContent("faq.md: unchanged, nothing to do.");
    expect(paste.getByTestId("knowledge-paste-content")).toHaveValue("same");
    unchanged = false;
    fireEvent.click(paste.getByTestId("knowledge-paste-submit"));
    await waitFor(() => expect(paste.getByTestId("knowledge-paste-result")).toHaveTextContent("unsupported content"));
  });
});

const inflight = (n: number) => Array.from({ length: n }, (_, i) => doc(`doc${i + 1}.md`, { status: "ingesting", chunk_count: 0, last_run_id: `run-live-${i + 1}` }));
const streamed = () => [...new Set(sent("GET", /^\/runs\/[^/]+\/stream$/).map((r) => apiPath(r.url).split("/")[2]))];
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

describe("live ingest progress: at most 3 streams, polling for the rest", () => {
  it("follows 3 of 5 in-flight runs' streams, shows each one's latest 'N of M sections embedded', and the 4th and 5th have none", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => json(200, { items: inflight(5), next_after: null }));
    await renderDetail();
    await waitFor(() => expect(streamed()).toEqual(["run-live-1", "run-live-2", "run-live-3"]));
    act(() => {
      streams.get("run-live-1")!.push({ type: "ingest_progress", data: { embedded: 2, total: 10 } });
      streams.get("run-live-1")!.push({ type: "ingest_progress", data: { embedded: 6, total: 10 } });
      streams.get("run-live-2")!.push({ type: "ingest_progress", data: { embedded: 1, total: 1 } });
    });
    await waitFor(() => expect(screen.getAllByTestId("knowledge-document-progress")).toHaveLength(2));
    const rows = screen.getAllByTestId("knowledge-document-row");
    expect(within(rows[0]).getByTestId("knowledge-document-progress")).toHaveTextContent("6 of 10 sections embedded");
    expect(within(rows[1]).getByTestId("knowledge-document-progress")).toHaveTextContent("1 of 1 section embedded");
    expect(within(rows[3]).queryByTestId("knowledge-document-progress")).toBeNull();
    expect(within(rows[4]).queryByTestId("knowledge-document-progress")).toBeNull();
    expect(streamed()).toHaveLength(3);
  });

  it("every in-flight document is re-read every 3 s (not before); one that is ready then shows ready and the header totals reload", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => json(200, { items: inflight(5), next_after: null }));
    on("GET", /^\/knowledge-bases\/kb1\/documents\/doc[45]\.md$/, (url) => json(200, doc(decodeURIComponent(url.pathname.split("/").pop()!), { status: "ready", chunk_count: 7 })));
    on("GET", /^\/knowledge-bases\/kb1\/documents\/doc[123]\.md$/, (url) => json(200, doc(decodeURIComponent(url.pathname.split("/").pop()!), { status: "ingesting", chunk_count: 0, last_run_id: "run-live-" + url.pathname.match(/doc(\d)/)![1] })));
    await renderDetail();
    const polled = () => sent("GET", /\/documents\/doc\d\.md$/).length;
    const detailReads = () => sent("GET", /^\/knowledge-bases\/kb1$/).length;
    const before = detailReads();
    await advance(2_999);
    expect(polled()).toBe(0);
    await advance(1);
    await waitFor(() => expect(polled()).toBe(5));
    await waitFor(() => expect(screen.getAllByTestId("knowledge-document-status").map((el) => el.getAttribute("data-status"))).toEqual(["ingesting", "ingesting", "ingesting", "ready", "ready"]));
    expect(detailReads()).toBeGreaterThan(before);
    // Only the three still in flight are polled again.
    await advance(3_000);
    await waitFor(() => expect(polled()).toBe(8));
  });

  it("a pending document shows Ingesting as soon as its run stream reports run_started, before the 3 s re-read", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => json(200, { items: [doc("doc1.md", { status: "pending", chunk_count: 0, last_run_id: "run-live-1" }), doc("doc2.md", { status: "pending", chunk_count: 0, last_run_id: "run-live-2" })], next_after: null }));
    await renderDetail();
    await waitFor(() => expect(streams.has("run-live-1")).toBe(true));
    const statuses = () => screen.getAllByTestId("knowledge-document-status").map((el) => [el.getAttribute("data-status"), el.textContent]);
    expect(statuses()).toEqual([["pending", "Pending"], ["pending", "Pending"]]);
    act(() => streams.get("run-live-1")!.push({ type: "run_started", data: { kind: "kb_ingest", mode: "ingest" } }));
    await waitFor(() => expect(statuses()).toEqual([["ingesting", "Ingesting"], ["pending", "Pending"]]));
    act(() => streams.get("run-live-2")!.push({ type: "ingest_progress", data: { phase: "chunked", embedded: 0, total: 4 } }));
    await waitFor(() => expect(statuses()).toEqual([["ingesting", "Ingesting"], ["ingesting", "Ingesting"]]));
    // No document re-read was needed for it.
    expect(sent("GET", /\/documents\/doc\d\.md$/)).toHaveLength(0);
  });

  it("a followed run that ends re-reads its document at once (ready, with its sections) and stops following it", async () => {
    on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => json(200, { items: inflight(1), next_after: null }));
    on("GET", /^\/knowledge-bases\/kb1\/documents\/doc1\.md$/, () => json(200, doc("doc1.md", { status: "ready", chunk_count: 12, last_run_id: "run-live-1" })));
    await renderDetail();
    await waitFor(() => expect(streams.has("run-live-1")).toBe(true));
    act(() => {
      streams.get("run-live-1")!.push({ type: "ingest_progress", data: { embedded: 12, total: 12 } });
      streams.get("run-live-1")!.push({ type: "run_completed", data: {} });
    });
    await waitFor(() => expect(within(screen.getByTestId("knowledge-document-row")).getByTestId("knowledge-document-status")).toHaveAttribute("data-status", "ready"));
    expect(within(screen.getByTestId("knowledge-document-row")).getByTestId("knowledge-document-sections")).toHaveTextContent("12");
    expect(screen.queryByTestId("knowledge-document-progress")).toBeNull();
    expect(streamed()).toEqual(["run-live-1"]);
  });
});

describe("documents: retry, deep link, Back, versions, metadata, delete", () => {
  it("Retry of a failed document posts reingest and the row goes pending; a refused retry shows the error and re-reads the row", async () => {
    on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => json(200, { items: [doc("billing.md", { status: "failed", error: "ingest_budget_exceeded", chunk_count: 0 })], next_after: null }));
    on("POST", /\/reingest$/, () => json(500, { detail: "queue unavailable" }));
    await renderDetail();
    expect(screen.getByTestId("knowledge-document-error")).toHaveTextContent("more tokens than allowed per run");
    fireEvent.click(screen.getByRole("button", { name: "Retry ingest of billing" }));
    expect(await screen.findByTestId("knowledge-documents-error")).toHaveTextContent("billing: queue unavailable");
    expect(sent("GET", /\/documents\/billing\.md$/).length).toBeGreaterThan(0);
    expect(screen.getByTestId("knowledge-document-retry")).toBeEnabled();
  });

  it("?doc= is a deep link: the Sheet opens on load with the document, its versions, metadata and a source link", async () => {
    nextRouter.reset(["/knowledge/kb1?doc=billing%20faq.md"]);
    on("GET", /^\/knowledge-bases\/kb1\/documents\/billing%20faq\.md$/, () => json(200, doc("billing faq.md", { title: "Billing FAQ", active_version: 2, latest_version: 2, source_url: "https://docs.example.com/billing", metadata: { lang: "en", tier: 3, beta: true } })));
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const sheet = await screen.findByTestId("knowledge-document-sheet");
    expect(await within(sheet).findByText("Billing FAQ")).toBeInTheDocument();
    expect(within(sheet).getByTestId("knowledge-document-versions")).toHaveTextContent("Active v2 · latest v2");
    expect(within(sheet).getByTestId("knowledge-document-metadata")).toHaveTextContent("lang: en");
    expect(within(sheet).getByTestId("knowledge-document-metadata")).toHaveTextContent("tier: 3");
    expect(within(sheet).getByTestId("knowledge-document-metadata")).toHaveTextContent("beta: true");
    expect(within(sheet).getByRole("link", { name: "https://docs.example.com/billing" })).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(nextRouter.url).toBe("/knowledge/kb1?doc=billing%20faq.md");
  });

  it("a source URL that is not http(s) is shown as no link; a document that does not exist says so", async () => {
    nextRouter.reset(["/knowledge/kb1?doc=install.md"]);
    on("GET", /^\/knowledge-bases\/kb1\/documents\/install\.md$/, () => json(200, doc("install.md", { source_url: "javascript:alert(1)" })));
    const first = render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const sheet = await screen.findByTestId("knowledge-document-sheet");
    await within(sheet).findByTestId("knowledge-document-versions");
    expect(sheet.querySelector('a[href^="javascript:"]')).toBeNull();
    expect(within(sheet).getByText("Source").nextElementSibling).toHaveTextContent("None");
    first.unmount();
    nextRouter.reset(["/knowledge/kb1?doc=ghost.md"]);
    on("GET", /\/documents\/ghost\.md$/, () => json(404, { detail: "Document not found" }));
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    expect(await screen.findByTestId("knowledge-document-not-found")).toBeInTheDocument();
  });

  it("opening a row pushes one history entry; Back closes the Sheet and returns to the list URL", async () => {
    await renderDetail();
    const row = screen.getAllByTestId("knowledge-document-row")[0];
    fireEvent.click(within(row).getByRole("button", { name: /install/ }));
    await act(() => nextRouter.settle());
    expect(await screen.findByTestId("knowledge-document-sheet")).toBeInTheDocument();
    expect(nextRouter.entries).toEqual(["/knowledge/kb1", "/knowledge/kb1?doc=install.md"]);
    act(() => nextRouter.back());
    await act(() => nextRouter.settle());
    await waitFor(() => expect(screen.queryByTestId("knowledge-document-sheet")).toBeNull());
    expect(nextRouter.url).toBe("/knowledge/kb1");
  });

  it("Show content loads it with include_content and previews it", async () => {
    nextRouter.reset(["/knowledge/kb1?doc=install.md"]);
    on("GET", /^\/knowledge-bases\/kb1\/documents\/install\.md$/, (url) => json(200, doc("install.md", url.searchParams.get("include_content") === "true" ? { content: "# Install\n\nRun it." } : {})));
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const sheet = await screen.findByTestId("knowledge-document-sheet");
    fireEvent.click(await within(sheet).findByTestId("knowledge-document-show-content"));
    expect(await within(sheet).findByTestId("knowledge-document-content")).toHaveTextContent("# Install Run it.");
  });

  it("Delete names one section in the singular", async () => {
    nextRouter.reset(["/knowledge/kb1?doc=install.md"]);
    on("GET", /^\/knowledge-bases\/kb1\/documents\/install\.md$/, () => json(200, doc("install.md", { chunk_count: 1 })));
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const sheet = await screen.findByTestId("knowledge-document-sheet");
    fireEvent.click(await within(sheet).findByTestId("knowledge-document-delete"));
    expect(await screen.findByText((_, el) => el?.tagName === "P" && /^Its 1 section is removed from search at once/.test(el.textContent ?? ""))).toBeInTheDocument();
  });

  it("Delete asks first (naming the sections), a failure keeps the Sheet and shows why, then it removes the document and closes", async () => {
    nextRouter.reset(["/knowledge/kb1?doc=install.md"]);
    let fail = true;
    on("GET", /^\/knowledge-bases\/kb1\/documents\/install\.md$/, () => json(200, doc("install.md")));
    on("DELETE", /\/documents\/install\.md$/, () => (fail ? json(500, { detail: "cannot delete now" }) : json(204, null)));
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const sheet = await screen.findByTestId("knowledge-document-sheet");
    fireEvent.click(await within(sheet).findByTestId("knowledge-document-delete"));
    expect(await screen.findByText((_, el) => el?.tagName === "P" && /^Its 4 sections are removed from search at once/.test(el.textContent ?? ""))).toBeInTheDocument();
    expect(sent("DELETE", /\/documents\//)).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Delete document" }));
    expect(await screen.findByText("cannot delete now")).toBeInTheDocument();
    expect(screen.getByTestId("knowledge-document-sheet")).toBeInTheDocument();
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Delete document" }));
    await waitFor(() => expect(screen.queryByTestId("knowledge-document-sheet")).toBeNull());
    expect(screen.getAllByTestId("knowledge-document-row")).toHaveLength(1);
  });
});

function estimate(extra: Record<string, unknown> = {}) {
  return { dry_run: true, knowledge_base_id: "kb1", embedding_model: "test:hash", dimensions: 1536, chunks: 9, approx_tokens: 1200, max_ingest_tokens: 2_000_000, exceeds_budget: false, estimated_cost_usd: "0.0012", price_source: "override", cost_note: null, reindex_pending: false, ...extra };
}

async function openReindex() {
  await renderDetail();
  fireEvent.click(screen.getByTestId("knowledge-reindex"));
  return within(await screen.findByTestId("knowledge-reindex-dialog"));
}

describe("reindex: estimate, budget, start, banner, cancel", () => {
  it("preselects the current model and shows the dry-run estimate; it posts dry_run only, and nothing starts", async () => {
    on("POST", /\/reindex$/, () => json(200, estimate()));
    const dialog = await openReindex();
    await waitFor(() => expect(dialog.getByTestId("knowledge-reindex-model")).toHaveValue("test:hash"));
    await waitFor(() => expect(dialog.getByTestId("knowledge-reindex-chunks")).toHaveTextContent("9"));
    expect(dialog.getByTestId("knowledge-reindex-cost")).toHaveTextContent("$0.0012");
    expect(dialog.getByTestId("knowledge-reindex-cost")).toHaveTextContent("override");
    expect(dialog.getByTestId("knowledge-reindex-tokens")).toHaveTextContent("1,200 of 2M allowed per run");
    expect(dialog.getByTestId("knowledge-reindex-start")).toBeEnabled();
    expect(dialog.queryByTestId("knowledge-reindex-over-budget")).toBeNull();
    expect(sent("POST", /\/reindex$/).map((r) => r.body)).toEqual([{ embedding_model: "test:hash", dry_run: true }]);
  });

  it("an unpriced model shows the API's cost note instead of a number", async () => {
    on("POST", /\/reindex$/, () => json(200, estimate({ estimated_cost_usd: null, price_source: null, cost_note: "no price for litellm:x" })));
    const dialog = await openReindex();
    await waitFor(() => expect(dialog.getByTestId("knowledge-reindex-cost")).toHaveTextContent("Unknown (no price for litellm:x)"));
  });

  it("over budget blocks Start with the reason; choosing another model re-estimates and unblocks it", async () => {
    on("POST", /\/reindex$/, (_url, init) => {
      const body = JSON.parse(String(init.body));
      return json(200, body.embedding_model === "test:hash" ? estimate({ approx_tokens: 3_000_000, exceeds_budget: true }) : estimate({ embedding_model: body.embedding_model, dimensions: 1024 }));
    });
    const dialog = await openReindex();
    await waitFor(() => expect(dialog.getByTestId("knowledge-reindex-over-budget")).toHaveTextContent(/EAP_KB_MAX_INGEST_TOKENS/));
    expect(dialog.getByTestId("knowledge-reindex-start")).toBeDisabled();
    fireEvent.change(dialog.getByTestId("knowledge-reindex-model"), { target: { value: "litellm:*" } });
    expect(dialog.getByTestId("knowledge-reindex-start")).toBeDisabled();
    fireEvent.change(dialog.getByLabelText("Model name on litellm"), { target: { value: "small-embed" } });
    await waitFor(() => expect(dialog.queryByTestId("knowledge-reindex-over-budget")).toBeNull());
    await waitFor(() => expect(dialog.getByTestId("knowledge-reindex-start")).toBeEnabled());
    expect(sent("POST", /\/reindex$/).at(-1)!.body).toEqual({ embedding_model: "litellm:small-embed", dry_run: true });
  });

  it("an estimate error is shown and Start stays disabled; a pending reindex is mentioned", async () => {
    on("POST", /\/reindex$/, () => json(422, { detail: "embedding model not allowed" }));
    const first = render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-document-row");
    fireEvent.click(screen.getByTestId("knowledge-reindex"));
    const dialog = within(await screen.findByTestId("knowledge-reindex-dialog"));
    expect(await dialog.findByText("embedding model not allowed")).toBeInTheDocument();
    expect(dialog.getByTestId("knowledge-reindex-start")).toBeDisabled();
    first.unmount();
    on("POST", /\/reindex$/, () => json(200, estimate({ reindex_pending: true })));
    const again = await openReindex();
    expect(await again.findByTestId("knowledge-reindex-pending")).toBeInTheDocument();
  });

  it("Start posts without dry_run and closes the dialog; a refusal (409) stays in it", async () => {
    let refuse = true;
    on("POST", /\/reindex$/, (_url, init) => {
      const body = JSON.parse(String(init.body));
      if (body.dry_run) return json(200, estimate());
      return refuse ? json(409, { detail: "a reindex to another model is pending" }) : json(202, { run_id: "rx1", kind: "kb_ingest", knowledge_base_id: "kb1" });
    });
    const dialog = await openReindex();
    await waitFor(() => expect(dialog.getByTestId("knowledge-reindex-start")).toBeEnabled());
    fireEvent.click(dialog.getByTestId("knowledge-reindex-start"));
    expect(await dialog.findByTestId("knowledge-reindex-error")).toHaveTextContent("a reindex to another model is pending");
    refuse = false;
    fireEvent.click(dialog.getByTestId("knowledge-reindex-start"));
    await waitFor(() => expect(screen.queryByTestId("knowledge-reindex-dialog")).toBeNull());
    expect(sent("POST", /\/reindex$/).filter((r) => !(r.body as { dry_run?: boolean }).dry_run).map((r) => r.body)).toEqual([{ embedding_model: "test:hash" }, { embedding_model: "test:hash" }]);
  });
});

describe("reindex: the banner, progress and cancel", () => {
  const reindexing = (extra: Record<string, unknown> = {}) => detail({ status: "reindexing", pending_generation: 2, pending_embedding_model: "litellm:qwen", pending_dimensions: 1024, last_generation: 2, ...extra });
  const runsWithReindex = () => on("GET", /^\/runs$/, () => json(200, { items: [{ run_id: "rx9", kind: "kb_ingest", knowledge_base_id: "kb1", status: "running", display_status: "running", input: JSON.stringify({ reindex: 2 }), output: null, error: null, created_at: T0, updated_at: T0, trace_id: null }], next_before: null }));

  it("a reindexing knowledge base opens with the banner, follows the reindex run's stream for progress, and Reindex is disabled", async () => {
    on("GET", /^\/knowledge-bases\/kb1$/, () => json(200, reindexing()));
    runsWithReindex();
    await renderDetail();
    const banner = screen.getByTestId("knowledge-reindex-banner");
    expect(banner).toHaveAttribute("data-status", "reindexing");
    expect(banner).toHaveTextContent("Reindexing with litellm:qwen");
    expect(banner).toHaveTextContent("Search uses the current embeddings until it finishes.");
    expect(screen.getByTestId("knowledge-reindex")).toBeDisabled();
    expect(within(screen.getByTestId("knowledge-summary")).getAllByTestId("knowledge-base-status")[0]).toHaveAttribute("data-status", "reindexing");
    await waitFor(() => expect(streams.has("rx9")).toBe(true));
    act(() => streams.get("rx9")!.push({ type: "ingest_progress", data: { embedded: 3, total: 9 } }));
    await waitFor(() => expect(banner).toHaveTextContent("3 of 9 sections embedded"));
    expect(within(banner).getByTestId("knowledge-reindex-trace")).toHaveAttribute("href", expect.stringContaining("trace=rx9"));
  });

  it("when the reindex run ends the knowledge base is re-read and the banner goes away", async () => {
    let done = false;
    on("GET", /^\/knowledge-bases\/kb1$/, () => json(200, done ? detail({ embedding_model: "litellm:qwen", dimensions: 1024, active_generation: 2 }) : reindexing()));
    runsWithReindex();
    await renderDetail();
    await waitFor(() => expect(streams.has("rx9")).toBe(true));
    done = true;
    act(() => streams.get("rx9")!.push({ type: "run_completed", data: {} }));
    await waitFor(() => expect(screen.queryByTestId("knowledge-reindex-banner")).toBeNull());
    expect(screen.getByTestId("knowledge-summary-model")).toHaveTextContent("litellm:qwen");
    expect(screen.getByTestId("knowledge-reindex")).toBeEnabled();
  });

  it("Cancel reindex sends DELETE and the banner goes; a refusal shows the error and keeps the banner", async () => {
    let cancelled = false;
    let refuse = true;
    on("GET", /^\/knowledge-bases\/kb1$/, () => json(200, cancelled ? detail() : reindexing()));
    on("DELETE", /\/reindex$/, () => (refuse ? json(409, { detail: "the reindex is already switching over" }) : (cancelled = true, json(204, null))));
    runsWithReindex();
    await renderDetail();
    fireEvent.click(screen.getByTestId("knowledge-reindex-cancel"));
    expect(await screen.findByTestId("knowledge-action-error")).toHaveTextContent("the reindex is already switching over");
    expect(screen.getByTestId("knowledge-reindex-banner")).toBeInTheDocument();
    refuse = false;
    fireEvent.click(screen.getByTestId("knowledge-reindex-cancel"));
    await waitFor(() => expect(screen.queryByTestId("knowledge-reindex-banner")).toBeNull());
    expect(sent("DELETE", /\/reindex$/)).toHaveLength(2);
  });

  it("a failed reindex shows the reason as an alert; Retry reindex restarts it with the pending model", async () => {
    on("GET", /^\/knowledge-bases\/kb1$/, () => json(200, reindexing({ status: "reindex_failed", error: "embedding_failed: provider down" })));
    on("POST", /\/reindex$/, () => json(202, { run_id: "rx10", kind: "kb_ingest", knowledge_base_id: "kb1" }));
    await renderDetail();
    const banner = screen.getByTestId("knowledge-reindex-banner");
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner).toHaveTextContent("The reindex with litellm:qwen failed: The embedding provider failed. (provider down)");
    fireEvent.click(within(banner).getByTestId("knowledge-reindex-retry"));
    await waitFor(() => expect(sent("POST", /\/reindex$/)).toHaveLength(1));
    expect(sent("POST", /\/reindex$/)[0].body).toEqual({ embedding_model: "litellm:qwen" });
  });

  it("starting a reindex from the dialog shows the banner at once", async () => {
    let started = false;
    on("GET", /^\/knowledge-bases\/kb1$/, () => json(200, started ? reindexing() : detail()));
    on("POST", /\/reindex$/, (_url, init) => (JSON.parse(String(init.body)).dry_run ? json(200, estimate()) : (started = true, json(202, { run_id: "rx11", kind: "kb_ingest", knowledge_base_id: "kb1" }))));
    const dialog = await openReindex();
    await waitFor(() => expect(dialog.getByTestId("knowledge-reindex-start")).toBeEnabled());
    fireEvent.click(dialog.getByTestId("knowledge-reindex-start"));
    expect(await screen.findByTestId("knowledge-reindex-banner")).toHaveAttribute("data-status", "reindexing");
    await waitFor(() => expect(streams.has("rx11")).toBe(true));
  });
});

function agent(id: string, name: string, draftKbs: string[], latest: number | null) {
  return { id, name, draft_spec: { spec_version: 1, name, instructions: "x", model: { provider: "test", name: "echo" }, tools: [], knowledge_bases: draftKbs.map((kbId) => ({ id: kbId })) }, latest_version: latest };
}
const version = (n: number, kbs: string[]) => json(200, { agent_id: "a", version: n, spec: { spec_version: 1, name: "x", instructions: "x", model: { provider: "test", name: "echo" }, tools: [], knowledge_bases: kbs.map((id) => ({ id })) }, spec_hash: "h", created_at: T0, created_by: "dev" });
const deletes = () => sent("DELETE", /^\/knowledge-bases\/kb1$/);

async function openDelete() {
  await renderDetail();
  fireEvent.click(screen.getByTestId("knowledge-delete"));
  return screen.findByTestId("knowledge-delete-references");
}

describe("deleting a knowledge base", () => {
  it("with no reference: the scan says so, the delete is sent without force, and the page goes to /knowledge", async () => {
    on("GET", /^\/agents$/, () => json(200, [agent("a1", "Unrelated", ["other"], null)]));
    on("DELETE", /^\/knowledge-bases\/kb1$/, () => json(204, null));
    const references = await openDelete();
    await waitFor(() => expect(references).toHaveTextContent("No agent attaches it."));
    expect(screen.getByText(/Its 2 documents and every section are deleted\./)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
    await waitFor(() => expect(deletes()).toHaveLength(1));
    expect(deletes()[0].url.search).toBe("");
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/knowledge");
  });

  it("the confirm waits for the scan: disabled while it runs, then enabled", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const fetchMock = vi.mocked(fetch);
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/api/v1/agents")) await gate;
      return base(input, init);
    });
    const references = await openDelete();
    expect(references).toHaveTextContent("Checking which agents attach it…");
    expect(screen.getByRole("button", { name: "Delete knowledge base" })).toBeDisabled();
    release();
    await waitFor(() => expect(screen.getByRole("button", { name: "Delete knowledge base" })).toBeEnabled());
  });

  it("the scan lists a draft-only reference and a published one (with its version) as links to the agents", async () => {
    on("GET", /^\/agents$/, () => json(200, [agent("a1", "Support", ["kb1"], 3), agent("a2", "Drafty", ["kb1"], null), agent("a3", "Other", [], 1)]));
    on("GET", /^\/agents\/a1\/versions\/3$/, () => version(3, ["kb1"]));
    on("GET", /^\/agents\/a3\/versions\/1$/, () => version(1, []));
    await openDelete();
    await waitFor(() => expect(screen.getAllByTestId("knowledge-delete-reference")).toHaveLength(2));
    const [drafty, support] = screen.getAllByTestId("knowledge-delete-reference").sort((a, b) => (a.textContent! < b.textContent! ? -1 : 1));
    expect(support).toHaveTextContent("Support");
    expect(support).toHaveTextContent("draft, v3");
    expect(within(support).getByRole("link", { name: "Support" })).toHaveAttribute("href", expect.stringContaining("a1"));
    expect(drafty).toHaveTextContent("draft");
  });

  it("a published reference found by the scan is still not forced: the first DELETE asks the API, and force needs its 409 list", async () => {
    on("GET", /^\/agents$/, () => json(200, [agent("a1", "Support", [], 3)]));
    on("GET", /^\/agents\/a1\/versions\/3$/, () => version(3, ["kb1"]));
    on("DELETE", /^\/knowledge-bases\/kb1$/, (url) => (url.searchParams.get("force") === "true" ? json(204, null) : json(409, { detail: "attached", agents: [{ agent_id: "a1", agent_name: "Support", version: 3, archived: false }] })));
    await openDelete();
    await screen.findByTestId("knowledge-delete-reference");
    fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
    await waitFor(() => expect(deletes()).toHaveLength(1));
    expect(deletes()[0].url.searchParams.get("force")).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "Delete anyway" }));
    await waitFor(() => expect(deletes()).toHaveLength(2));
    expect(deletes()[1].url.searchParams.get("force")).toBe("true");
  });

  it("an archived agent the scan cannot see (GET /agents leaves it out) is revealed only by the 409; nothing is deleted without Delete anyway", async () => {
    on("GET", /^\/agents$/, () => json(200, [agent("a3", "Other", [], null)]));
    let deleted = false;
    on("DELETE", /^\/knowledge-bases\/kb1$/, (url) => {
      if (url.searchParams.get("force") === "true") {
        deleted = true;
        return json(204, null);
      }
      return json(409, { detail: "attached", agents: [{ agent_id: "a9", agent_name: "Retired bot", version: 2, archived: true }] });
    });
    const references = await openDelete();
    await waitFor(() => expect(references).toHaveTextContent("No agent attaches it."));
    fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
    await waitFor(() => expect(screen.getByTestId("knowledge-delete-reference")).toHaveTextContent("Retired bot"));
    expect(screen.getByTestId("knowledge-delete-reference")).toHaveTextContent("v2 (archived)");
    expect(deletes()).toHaveLength(1);
    expect(deletes()[0].url.searchParams.get("force")).toBeNull();
    expect(deleted).toBe(false);
    expect(nextRouter.url).not.toBe("/knowledge");
    fireEvent.click(screen.getByRole("button", { name: "Delete anyway" }));
    await waitFor(() => expect(deleted).toBe(true));
  });

  it("a draft-only reference is not forced: the API decides", async () => {
    on("GET", /^\/agents$/, () => json(200, [agent("a2", "Drafty", ["kb1"], null)]));
    on("DELETE", /^\/knowledge-bases\/kb1$/, () => json(204, null));
    await openDelete();
    await screen.findByTestId("knowledge-delete-reference");
    fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
    await waitFor(() => expect(deletes()).toHaveLength(1));
    expect(deletes()[0].url.searchParams.get("force")).toBeNull();
  });

  it("the API's 409 can list a draft-only agent (version null, draft true): it shows as \"(draft)\"", async () => {
    on("DELETE", /^\/knowledge-bases\/kb1$/, (url) => (url.searchParams.get("force") === "true" ? json(204, null) : json(409, { detail: "attached", agents: [{ agent_id: "a1", agent_name: "Support", version: 3, archived: false, draft: false }, { agent_id: "a3", agent_name: "Drafty", version: null, archived: false, draft: true }] })));
    const references = await openDelete();
    await waitFor(() => expect(references).toHaveTextContent("No agent attaches it."));
    fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
    await waitFor(() => expect(screen.getAllByTestId("knowledge-delete-reference")).toHaveLength(2));
    expect(references).toHaveTextContent("These 2 agents attach it (their latest published version, or their draft)");
    const [published, draft] = screen.getAllByTestId("knowledge-delete-reference");
    expect(published).toHaveTextContent("Support v3");
    expect(draft).toHaveTextContent("Drafty (draft)");
    expect(draft).not.toHaveTextContent(/v\d|vnull/);
    expect(within(draft).getByRole("link", { name: "Drafty" })).toHaveAttribute("href", expect.stringContaining("/agents/a3"));
  });

  it("when the scan missed it, the API's 409 lists the agents and turns the button into Delete anyway, which forces", async () => {
    on("DELETE", /^\/knowledge-bases\/kb1$/, (url) => (url.searchParams.get("force") === "true" ? json(204, null) : json(409, { detail: "attached", agents: [{ agent_id: "a1", agent_name: "Support", version: 3, archived: false }, { agent_id: "a2", agent_name: "Old bot", version: 1, archived: true }] })));
    const references = await openDelete();
    await waitFor(() => expect(references).toHaveTextContent("No agent attaches it."));
    fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
    await waitFor(() => expect(screen.getAllByTestId("knowledge-delete-reference")).toHaveLength(2));
    expect(references).toHaveTextContent("These 2 agents' latest published version attaches it");
    expect(screen.getAllByTestId("knowledge-delete-reference")[1]).toHaveTextContent("v1 (archived)");
    expect(screen.queryByRole("button", { name: "Delete knowledge base" })).toBeNull();
    expect(deletes()).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Delete anyway" }));
    await waitFor(() => expect(deletes()).toHaveLength(2));
    expect(deletes()[1].url.searchParams.get("force")).toBe("true");
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/knowledge");
  });
});

describe("deleting a knowledge base: failures", () => {
  it("a scan that cannot list agents says so; Delete still works and the API's 409 backs it up", async () => {
    on("GET", /^\/agents$/, () => json(500, { detail: "agents down" }));
    on("DELETE", /^\/knowledge-bases\/kb1$/, () => json(204, null));
    const references = await openDelete();
    await waitFor(() => expect(references).toHaveTextContent("Could not check which agents attach it."));
    fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
    await waitFor(() => expect(deletes()).toHaveLength(1));
  });

  it("an unreadable published version is flagged as partial", async () => {
    on("GET", /^\/agents$/, () => json(200, [agent("a1", "Support", [], 3), agent("a2", "Drafty", ["kb1"], null)]));
    on("GET", /^\/agents\/a1\/versions\/3$/, () => json(500, { detail: "boom" }));
    await openDelete();
    await waitFor(() => expect(screen.getByTestId("knowledge-delete-references")).toHaveTextContent("Some published versions could not be read"));
    expect(screen.getAllByTestId("knowledge-delete-reference")).toHaveLength(1);
  });

  it("a server error stays in the dialog; Cancel closes it with nothing deleted; an already-gone 404 counts as deleted", async () => {
    let gone = false;
    on("DELETE", /^\/knowledge-bases\/kb1$/, () => (gone ? json(404, { detail: "Knowledge base not found" }) : json(500, { detail: "database busy" })));
    const references = await openDelete();
    await waitFor(() => expect(references).toHaveTextContent("No agent attaches it."));
    fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
    expect(await screen.findByText("database busy")).toBeInTheDocument();
    expect(nextRouter.url).toBe("/knowledge/kb1");
    gone = true;
    fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
    await waitFor(() => expect(deletes()).toHaveLength(2));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/knowledge");
  });

  it("Cancel sends nothing", async () => {
    await openDelete();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByTestId("knowledge-delete-references")).toBeNull());
    expect(deletes()).toHaveLength(0);
  });
});

describe("deleteKnowledgeBase (client)", () => {
  it("a 409 with agents throws KnowledgeBaseInUseError carrying them; a 409 without them is an ApiError; force is a query flag", async () => {
    on("DELETE", /^\/knowledge-bases\/inuse$/, () => json(409, { detail: "attached to 1 agent(s)", agents: [{ agent_id: "a1", agent_name: "S", version: 2, archived: false }] }));
    on("DELETE", /^\/knowledge-bases\/odd$/, () => json(409, { detail: "something else" }));
    on("DELETE", /^\/knowledge-bases\/ok$/, () => json(204, null));
    const err = await deleteKnowledgeBase("inuse").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(KnowledgeBaseInUseError);
    expect((err as KnowledgeBaseInUseError).agents).toEqual([{ agent_id: "a1", agent_name: "S", version: 2, archived: false }]);
    expect((err as KnowledgeBaseInUseError).status).toBe(409);
    const other = await deleteKnowledgeBase("odd").catch((e: unknown) => e);
    expect(other).not.toBeInstanceOf(KnowledgeBaseInUseError);
    expect((other as Error).message).toBe("something else");
    await deleteKnowledgeBase("ok", { force: true });
    expect(sent("DELETE", /^\/knowledge-bases\/ok$/)[0].url.search).toBe("?force=true");
  });
});

function hit(n: number, extra: Record<string, unknown> = {}) {
  return { n, title: `Section ${n}`, url: null, ref: `kb:doc${n}.md`, kb_id: "kb1", document_id: `d${n}`, document_key: `doc${n}.md`, chunk_id: `c${n}`, doc_version: 1, score: 0.5, content: `Body ${n}`, ...extra };
}
const searchOk = (hits: unknown[], runId = "s1") => json(200, { run_id: runId, status: "completed", trace_id: null, result: { knowledge_base_id: "kb1", embedding_model: "test:hash", hits } });

async function search(query = "refund", fields: Record<string, string> = {}, fakeTimers: Parameters<typeof vi.useFakeTimers>[0] | null = null) {
  await renderDetail("search");
  const form = await screen.findByTestId("knowledge-search-form");
  fireEvent.change(within(form).getByTestId("knowledge-search-query"), { target: { value: query } });
  const ids: Record<string, string> = { topK: "knowledge-search-top-k", minScore: "knowledge-search-min-score", filter: "knowledge-search-filter" };
  for (const [name, value] of Object.entries(fields)) fireEvent.change(within(form).getByTestId(ids[name]), { target: { value } });
  // The page is up on real timers; only the search's own wait runs on the fake clock.
  if (fakeTimers) vi.useFakeTimers(fakeTimers);
  fireEvent.click(within(form).getByTestId("knowledge-search-submit"));
  return form;
}

describe("search tab: inputs", () => {
  it("sends the defaults (top_k 8, no min score, no filter) and a filter whose values may be lists", async () => {
    on("POST", /\/search$/, () => searchOk([hit(1)]));
    await search("refund");
    await screen.findAllByTestId("knowledge-search-result");
    expect(sent("POST", /\/search$/)[0].body).toEqual({ query: "refund", top_k: 8, min_score: null, filter: null });
    cleanup();
    await search("again", { filter: '{"tier": ["a", "b"], "n": 2, "ok": true}', minScore: "-0.5", topK: "20" });
    await waitFor(() => expect(sent("POST", /\/search$/)).toHaveLength(2));
    expect(sent("POST", /\/search$/)[1].body).toEqual({ query: "again", top_k: 20, min_score: -0.5, filter: { tier: ["a", "b"], n: 2, ok: true } });
  });

  it.each([
    ["an empty query", "  ", {}, "Type a query."],
    ["a filter that is not JSON", "q", { filter: "lang = en" }, "The filter is not valid JSON."],
    ["a filter with a nested object", "q", { filter: '{"a": {"b": 1}}' }, '"a" must be a string, number, boolean or a list of them.'],
    ["a filter list with an object", "q", { filter: '{"a": [1, {"x": 1}]}' }, '"a" must be a string, number, boolean or a list of them.'],
  ])("refuses %s before any request", async (_name, query, fields, message) => {
    await search(query, fields);
    expect(await screen.findByTestId("knowledge-search-error")).toHaveTextContent(message);
    expect(sent("POST", /\/search$/)).toHaveLength(0);
  });
});

describe("search tab: numeric fields", () => {
  // The number inputs carry min, max and step, so the browser itself blocks an out-of-range submit.
  it.each([
    ["top_k 0", { topK: "0" }, "knowledge-search-top-k"],
    ["top_k 21", { topK: "21" }, "knowledge-search-top-k"],
    ["a fractional top_k", { topK: "2.5" }, "knowledge-search-top-k"],
    ["a min score above 1", { minScore: "1.5" }, "knowledge-search-min-score"],
    ["a min score below -1", { minScore: "-2" }, "knowledge-search-min-score"],
  ])("%s is invalid and nothing is sent", async (_name, fields, testId) => {
    await search("q", fields);
    expect((screen.getByTestId(testId) as HTMLInputElement).checkValidity()).toBe(false);
    expect(sent("POST", /\/search$/)).toHaveLength(0);
  });
});

describe("search tab: results", () => {
  it("shows each hit's number, title, heading path, score (3 decimals), text, metadata, and a safe URL link or its Studio document", async () => {
    const long = "x".repeat(700);
    on("POST", /\/search$/, () => searchOk([
      hit(1, { score: 0.91234, url: "https://docs.example.com/a#b", heading_path: ["Guide", "Setup"], metadata: { lang: "en", n: 2 } }),
      hit(2, { score: 0.1, url: "javascript:alert(1)", document_key: "evil.md" }),
      hit(3, { score: 0.05, url: "data:text/html,<b>x", content: long, content_truncated: true }),
    ]));
    await search();
    const results = await screen.findAllByTestId("knowledge-search-result");
    expect(screen.getByText("3 results")).toBeInTheDocument();
    expect(results[0]).toHaveTextContent("[1]");
    expect(results[0]).toHaveTextContent("Guide › Setup");
    expect(within(results[0]).getByTestId("knowledge-search-score")).toHaveTextContent("0.912");
    expect(within(results[0]).getByTestId("knowledge-search-url")).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(results[0]).toHaveTextContent("lang: en");
    expect(results[0]).toHaveTextContent("n: 2");
    expect(within(results[1]).getByTestId("knowledge-search-document")).toHaveAttribute("href", "/knowledge/kb1?doc=evil.md");
    expect(within(results[2]).getByTestId("knowledge-search-document")).toHaveAttribute("href", "/knowledge/kb1?doc=doc3.md");
    expect(within(results[2]).getByText(/^x{600}…$/)).toBeInTheDocument();
    expect(document.querySelector('a[href^="javascript:"], a[href^="data:"]')).toBeNull();
  });

  it("the trace link opens the trace Sheet in place and keeps the Search tab", async () => {
    on("POST", /\/search$/, () => searchOk([hit(1)], "s77"));
    await search();
    await screen.findAllByTestId("knowledge-search-result");
    fireEvent.click(screen.getByTestId("knowledge-search-trace"));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/knowledge/kb1?tab=search&trace=s77");
    expect(await screen.findByTestId("trace-sheet")).toBeInTheDocument();
  });

  it("no hits is an empty state (with the trace link); a run that failed shows its error; an HTTP error shows in the form", async () => {
    on("POST", /\/search$/, () => searchOk([], "s2"));
    await search();
    expect(await screen.findByTestId("knowledge-search-empty")).toHaveTextContent("No sections matched");
    expect(screen.getByText("0 results")).toBeInTheDocument();
    expect(screen.getByTestId("knowledge-search-trace")).toBeInTheDocument();
    cleanup();
    on("POST", /\/search$/, () => json(200, { run_id: "s3", status: "failed", trace_id: null, error: "embedding provider timed out" }));
    await search();
    const failed = await screen.findByTestId("knowledge-search-results");
    expect(failed).toHaveAttribute("data-status", "failed");
    expect(within(failed).getByRole("alert")).toHaveTextContent("embedding provider timed out");
    expect(within(failed).getByTestId("knowledge-search-trace")).toBeInTheDocument();
    cleanup();
    on("POST", /\/search$/, () => json(422, { detail: "the filter key is not allowed" }));
    await search();
    expect(await screen.findByTestId("knowledge-search-error")).toHaveTextContent("the filter key is not allowed");
  });
});

const CLOCK = { toFake: ["setTimeout", "clearTimeout", "Date"] as ("setTimeout" | "clearTimeout" | "Date")[] };

describe("search tab: a search that is still running (202) is polled, for at most 30 s", () => {
  const pending = () => json(202, { run_id: "sp", status: "running", trace_id: null });
  const polls = () => sent("GET", /^\/runs\/sp$/).length;

  it("polls the run each second and shows the results when it completes", async () => {
    let state: Record<string, unknown> = { run_id: "sp", status: "running" };
    on("POST", /\/search$/, pending);
    on("GET", /^\/runs\/sp$/, () => json(200, state));
    await search("refund", {}, CLOCK);
    await advance(0);
    expect(screen.getByText("Searching…")).toBeInTheDocument();
    await advance(3_000);
    expect(polls()).toBe(3);
    expect(screen.queryByTestId("knowledge-search-results")).toBeNull();
    state = { run_id: "sp", status: "completed", trace_id: null, output: JSON.stringify({ knowledge_base_id: "kb1", embedding_model: "test:hash", hits: [hit(1, { score: 0.8 })] }) };
    await advance(1_000);
    expect(screen.getAllByTestId("knowledge-search-result")).toHaveLength(1);
    expect(screen.getByTestId("knowledge-search-score")).toHaveTextContent("0.800");
    expect(screen.queryByText("Searching…")).toBeNull();
    const total = polls();
    await advance(5_000);
    expect(polls()).toBe(total);
  });

  it("gives up after 30 s with a message and the trace link, and stops polling", async () => {
    on("POST", /\/search$/, pending);
    on("GET", /^\/runs\/sp$/, () => json(200, { run_id: "sp", status: "running" }));
    await search("refund", {}, CLOCK);
    await advance(29_000);
    expect(screen.queryByTestId("knowledge-search-results")).toBeNull();
    expect(polls()).toBeLessThanOrEqual(29);
    await advance(2_000);
    const results = screen.getByTestId("knowledge-search-results");
    expect(results).toHaveAttribute("data-status", "failed");
    expect(within(results).getByRole("alert")).toHaveTextContent("The search is still running; open its trace to follow it.");
    expect(within(results).getByTestId("knowledge-search-trace")).toBeInTheDocument();
    const total = polls();
    expect(total).toBeGreaterThanOrEqual(29);
    expect(total).toBeLessThanOrEqual(31);
    await advance(30_000);
    expect(polls()).toBe(total);
  });

  it("a polled run that fails ends the wait with its error", async () => {
    on("POST", /\/search$/, pending);
    on("GET", /^\/runs\/sp$/, () => json(200, { run_id: "sp", status: "failed", error: "worker crashed", trace_id: null }));
    await search("refund", {}, CLOCK);
    await advance(1_000);
    expect(screen.getByRole("alert")).toHaveTextContent("worker crashed");
  });
});

function ingestRun(id: string, input: unknown, extra: Record<string, unknown> = {}) {
  return { run_id: id, kind: "kb_ingest", knowledge_base_id: "kb1", agent_id: null, version: null, status: "completed", display_status: "completed", input: JSON.stringify(input), output: null, error: null, heartbeat_at: null, created_at: T0, updated_at: "2026-11-14T10:00:05Z", trace_id: null, conversation_id: null, turn: null, ...extra };
}

describe("ingest runs tab", () => {
  it("shows status, what each run did and its duration; Open and the row both open the trace Sheet in place, keeping ?tab=runs", async () => {
    on("GET", /^\/runs$/, () => json(200, { items: [ingestRun("run-a", { document_id: "d-install.md", doc_version: 2 }), ingestRun("run-b", { reindex: 3 }, { status: "running", display_status: "running" }), ingestRun("run-c", { document_id: "d-gone", doc_version: 1 }, { status: "failed", display_status: "failed" })], next_before: null }));
    await renderDetail("runs");
    const rows = await screen.findAllByTestId("knowledge-run-row");
    await waitFor(() => expect(within(rows[0]).getByTestId("knowledge-run-mode")).toHaveTextContent("install v2"));
    expect(within(rows[1]).getByTestId("knowledge-run-mode")).toHaveTextContent("Reindex (generation 3)");
    expect(within(rows[1]).getByText("in progress")).toBeInTheDocument();
    expect(within(rows[0]).getByText("5.00 s")).toBeInTheDocument();
    expect(within(rows[2]).getByTestId("knowledge-run-mode")).toHaveTextContent("Document v1");
    fireEvent.click(within(rows[2]).getByTestId("knowledge-run-trace"));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/knowledge/kb1?tab=runs&trace=run-c");
    expect(await screen.findByTestId("trace-sheet")).toBeInTheDocument();
    expect(screen.getByTestId("knowledge-runs-tab")).toBeInTheDocument();
  });

  it("an empty list says so; Load more pages by cursor; a failure has Retry", async () => {
    on("GET", /^\/runs$/, () => json(200, { items: [], next_before: null }));
    await renderDetail("runs");
    expect(await screen.findByText("No ingest runs yet")).toBeInTheDocument();
    cleanup();
    on("GET", /^\/runs$/, (url) => (url.searchParams.get("before") ? json(200, { items: [ingestRun("run-old", { reindex: 1 })], next_before: null }) : json(200, { items: [ingestRun("run-new", { reindex: 2 })], next_before: "cur1" })));
    await renderDetail("runs");
    fireEvent.click(await screen.findByTestId("knowledge-runs-more"));
    await waitFor(() => expect(screen.getAllByTestId("knowledge-run-row")).toHaveLength(2));
    expect(screen.queryByTestId("knowledge-runs-more")).toBeNull();
    cleanup();
    let fail = true;
    on("GET", /^\/runs$/, () => (fail ? json(500, { detail: "runs down" }) : json(200, { items: [ingestRun("run-x", { reindex: 1 })], next_before: null })));
    await renderDetail("runs");
    const error = await screen.findByTestId("datalist-error");
    expect(error).toHaveTextContent("runs down");
    fail = false;
    fireEvent.click(within(error).getByRole("button", { name: "Retry" }));
    expect(await screen.findAllByTestId("knowledge-run-row")).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------------------------
// Citations in the chat's assistant text

const SRC: CitationSource[] = [
  { n: 1, title: "Install guide", url: "https://docs.example.com/install#setup", kb_id: "kb1", document_key: "install.md" },
  { n: 2, title: "Billing", url: null, kb_id: "kb1", document_key: "billing.md" },
  { n: 3, title: "Refunds", url: "http://docs.example.com/refunds", kb_id: "kb1", document_key: "refunds.md" },
];

function turn(events: Array<[string, string, Record<string, unknown>]>): Map<string, ToolCallInfo> {
  const list: RunEvent[] = events.map(([type, name, data], i) => ({ seq: i + 1, type: type as RunEvent["type"], data: { tool_name: name, ...data }, ts: `2026-11-14T10:00:0${i}Z` }));
  const map = new Map<string, ToolCallInfo>();
  for (const [id, info] of toolCallInfos(list, false)) map.set(toolCallInfoKey("run-1", id), info);
  return map;
}

const retrieveCall = (id: string, sources: unknown) => [
  ["tool_call", "retrieve", { tool_call_id: id }],
  ["tool_result", "retrieve", { tool_call_id: id, status: "ok", content: "...", sources }],
] as Array<[string, string, Record<string, unknown>]>;

function say(text: string, infos: Map<string, ToolCallInfo> = turn(retrieveCall("c1", SRC))) {
  return render(
    <ToolCallInfoContext.Provider value={infos}>
      <MarkdownText {...({ type: "text", text, status: { type: "complete" } } as unknown as React.ComponentProps<typeof MarkdownText>)} />
    </ToolCallInfoContext.Provider>,
  );
}
const cites = () => screen.queryAllByTestId("citation-link");
const hrefs = () => cites().map((a) => a.getAttribute("href"));
const noEvil = () => expect(document.querySelector('a[href*="evil"], img[src*="evil"]')).toBeNull();

describe("citations: [n] maps to the right source", () => {
  it("each [n] links to its own source, in order, with the source's title as the accessible name", () => {
    say("Install [1], billing [2], refunds [3]; again [1].");
    expect(hrefs()).toEqual(["https://docs.example.com/install#setup", "/knowledge/kb1?doc=billing.md", "http://docs.example.com/refunds", "https://docs.example.com/install#setup"]);
    expect(cites().map((a) => a.getAttribute("aria-label"))).toEqual(["Source 1: Install guide", "Source 2: Billing", "Source 3: Refunds", "Source 1: Install guide"]);
    expect(cites().map((a) => a.getAttribute("target"))).toEqual(["_blank", null, "_blank", "_blank"]);
    expect(cites().map((a) => a.textContent)).toEqual(["[1]", "[2]", "[3]", "[1]"]);
    for (const a of cites().filter((el) => el.getAttribute("target"))) expect(a.getAttribute("rel")).toMatch(/noopener.*noreferrer|noreferrer.*noopener/);
  });

  it("sources numbered out of order map by their n, not by position", () => {
    say("See [2] then [1].", turn(retrieveCall("c1", [SRC[1], SRC[0]])));
    expect(hrefs()).toEqual(["/knowledge/kb1?doc=billing.md", "https://docs.example.com/install#setup"]);
  });

  it("[n] inside bold, a list item and a table cell still links; inside code it does not", () => {
    say("**Note [1]**\n\n- item [2]\n\n| a |\n|---|\n| cell [3] |\n\n`[1]`\n\n```\n[2]\n```");
    expect(cites()).toHaveLength(3);
    expect(screen.getAllByText(/^\[[12]\]$/).filter((el) => el.tagName === "CODE")).toHaveLength(2);
  });
});

describe("citations: link safety", () => {
  it.each(["javascript:alert(1)", "JaVaScRiPt:alert(1)", "  javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)", "file:///etc/passwd", "//evil.test/x", "/relative/path", "ftp://evil.test/x", "not a url", ""])(
    "a source URL of %j is never linked: [1] goes to the document in Studio",
    (url) => {
      say("Claim [1].", turn(retrieveCall("c1", [{ ...SRC[0], url }])));
      expect(hrefs()).toEqual(["/knowledge/kb1?doc=install.md"]);
      expect(document.querySelector('a[href^="javascript:" i], a[href^="data:" i], a[href^="vbscript:" i], a[href^="file:" i], a[href^="//"], a[href^="ftp:"]')).toBeNull();
    },
  );

  it("a source with an unsafe URL and no document stays plain text", () => {
    say("Claim [1].", turn(retrieveCall("c1", [{ n: 1, title: "x", url: "javascript:alert(1)" }])));
    expect(cites()).toHaveLength(0);
    expect(screen.getByText("Claim [1].")).toBeInTheDocument();
  });

  it("model-written [1](url), [ 1 ](url), [[1]](url) and a [1]: url definition never keep the model's URL", () => {
    say("A [1](https://evil.test/a) B [ 2 ](https://evil.test/b) C [[3]](https://evil.test/c)\n\nD [1] and E [2][2]\n\n[1]: https://evil.test/d\n[2]: https://evil.test/e");
    noEvil();
    expect(hrefs().sort()).toEqual(["/knowledge/kb1?doc=billing.md", "/knowledge/kb1?doc=billing.md", "http://docs.example.com/refunds", "https://docs.example.com/install#setup", "https://docs.example.com/install#setup"]);
  });

  it("model-written javascript: and data: citation links are rewritten too", () => {
    say("[1](javascript:alert(1)) [2](data:text/html,x) [[3]](javascript:alert(2))");
    expect(document.querySelector('a[href^="javascript:"], a[href^="data:"]')).toBeNull();
    expect(cites()).toHaveLength(3);
  });

  it("raw HTML anchors in the answer are dropped, not rendered", () => {
    say('Look <a href="https://evil.test">[1]</a> and <img src="https://evil.test/p.png">.');
    noEvil();
  });

  it("a link that is not a citation keeps the model's URL (the rewrite is only for citation-shaped links)", () => {
    say("Read [the docs](https://example.org/docs) and [source 1](https://example.org/s1).");
    expect(screen.getByRole("link", { name: "the docs" })).toHaveAttribute("href", "https://example.org/docs");
    expect(screen.getByRole("link", { name: "source 1" })).toHaveAttribute("href", "https://example.org/s1");
    expect(cites()).toHaveLength(0);
  });
});

describe("citations: which turns and which retrieve", () => {
  it("a turn that never called retrieve gets no citation links (and model links are kept as written)", () => {
    const noRetrieve = turn([
      ["tool_call", "http.get", { tool_call_id: "c1" }],
      ["tool_result", "http.get", { tool_call_id: "c1", status: "ok", content: "x", sources: [{ n: 1, title: "forged", url: "https://evil.test" }] }],
    ]);
    say("Fact [1] and [2](https://example.org/two).", noRetrieve);
    expect(cites()).toHaveLength(0);
    expect(screen.getByText(/Fact \[1\] and/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "2" })).toHaveAttribute("href", "https://example.org/two");
    noEvil();
  });

  it("a turn with no tool calls at all renders [n] as text", () => {
    say("Fact [1].", new Map());
    expect(cites()).toHaveLength(0);
    expect(screen.getByText("Fact [1].")).toBeInTheDocument();
  });

  it("a retrieve that found nothing leaves [n] as plain text", () => {
    say("Fact [1].", turn(retrieveCall("c1", [])));
    expect(cites()).toHaveLength(0);
  });

  it("several retrieve calls: the last one's sources win; its numbering is not mixed with the earlier call's", () => {
    const first = [{ n: 1, title: "Old 1", url: "https://old.example.com/1" }, { n: 2, title: "Old 2", url: "https://old.example.com/2" }];
    const last = [{ n: 1, title: "New 1", url: "https://new.example.com/1" }];
    say("A [1] B [2].", turn([...retrieveCall("c1", first), ["tool_call", "http.get", { tool_call_id: "c2" }], ...retrieveCall("c3", last)]));
    expect(hrefs()).toEqual(["https://new.example.com/1"]);
    expect(cites()[0]).toHaveAccessibleName("Source 1: New 1");
    expect(screen.getByText(/B \[2\]\./)).toBeInTheDocument();
  });

  it("when the last retrieve found nothing, the earlier call's sources still serve the answer", () => {
    say("A [1].", turn([...retrieveCall("c1", [{ n: 1, title: "Old", url: "https://old.example.com/1" }]), ...retrieveCall("c2", [])]));
    expect(hrefs()).toEqual(["https://old.example.com/1"]);
  });

  it("another turn's retrieve does not give this turn citations", () => {
    const other = new Map<string, ToolCallInfo>([[toolCallInfoKey("run-2", "c1"), { status: "ok", durationMs: 1, sources: SRC }]]);
    say("Fact [1].", other);
    expect(cites()).toHaveLength(0);
  });

  it("[n] with no source (0, beyond the last, four digits) stays plain text while valid ones link", () => {
    say("Zero [0], valid [2], past the end [4], far [9], huge [1234].");
    expect(hrefs()).toEqual(["/knowledge/kb1?doc=billing.md"]);
    expect(screen.getByText(/Zero \[0\], valid/)).toBeInTheDocument();
    expect(screen.getByText(/past the end \[4\], far \[9\], huge \[1234\]\.$/)).toBeInTheDocument();
  });

  it("malformed sources (no number, a zero, not an object) are dropped; the rest still cite", () => {
    say("A [1] B [2] C [3]", turn(retrieveCall("c1", [{ title: "no n" }, null, "x", { n: 0, title: "zero" }, { n: 2, title: "Two", url: "https://two.example.com/" }])));
    expect(hrefs()).toEqual(["https://two.example.com/"]);
  });
});

describe("kb_ingest runs in the trace Sheet", () => {
  const RUN = ingestRun("run-a", { document_id: "d-install.md", doc_version: 1 });
  function stubTrace(run: Record<string, unknown>) {
    on("GET", /^\/runs$/, () => json(200, { items: [run], next_before: null }));
    on("GET", /^\/runs\/run-a$/, () => json(200, run));
    on("GET", /^\/runs\/run-a\/events$/, () => json(200, []));
    on("GET", /^\/runs\/run-a\/trace$/, () => json(200, {
      run,
      spans: [{ id: "root", parent_id: null, kind: "run", name: "kb ingest", start: T0, end: "2026-11-14T10:00:05Z", status: "ok", attributes: { run_kind: "kb_ingest", run_id: "run-a" }, input: null, output: null, truncated: {} }],
      totals: { runs: 1, model_requests: 0, tool_calls: 0, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null, cost_usd: null, unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 5000, status: "completed" },
      truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
      limits: { max_spans: 2000, max_events_per_run: 10000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
    }));
  }

  it("the Sheet's Events tab names the run a knowledge ingest and links 'Open knowledge base' to the Ingest runs tab", async () => {
    stubTrace(RUN);
    nextRouter.reset(["/knowledge/kb1?tab=runs&trace=run-a&traceTab=events"]);
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const sheet = await screen.findByTestId("trace-sheet");
    const link = await within(sheet).findByTestId("run-subject-knowledge-base");
    expect(link).toHaveAttribute("title", "Open knowledge base");
    expect(link).toHaveAttribute("href", "/knowledge/kb1?tab=runs");
    expect(within(sheet).getByTestId("trace-sheet-meta")).toHaveTextContent("Knowledge ingest run");
    expect(within(sheet).getByRole("tab", { name: "Events" })).toHaveAttribute("aria-selected", "true");
  });

  it("a kb_ingest run without a knowledge base id (older API) is plain text, not a link", async () => {
    stubTrace({ ...RUN, knowledge_base_id: null });
    nextRouter.reset(["/knowledge/kb1?tab=runs&trace=run-a&traceTab=events"]);
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(await within(sheet).findByTestId("run-subject")).toHaveAttribute("data-kind", "kb_ingest");
    expect(within(sheet).queryByTestId("run-subject-knowledge-base")).toBeNull();
  });

  it("the Sheet's tab parameter is traceTab: ?tab stays the page's own tab (Ingest runs) while the Sheet is on Events", async () => {
    stubTrace(RUN);
    nextRouter.reset(["/knowledge/kb1?tab=runs&trace=run-a&traceTab=events"]);
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    await screen.findByTestId("trace-sheet");
    expect(screen.getByTestId("knowledge-tab-runs")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("knowledge-runs-tab")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------------------------
// Navigation, shortcuts, palette

const sidebarLink = () => document.querySelector<HTMLAnchorElement>('[data-testid="sidebar"] a[data-nav-id="knowledge"]');

describe("navigation: sidebar, g k, c and the palette", () => {
  beforeEach(() => {
    for (const path of [/^\/workflows$/, /^\/tools$/, /^\/mcp-servers$/]) on("GET", path, () => json(200, []));
  });

  it("Knowledge is a Primitives item after Processors with the g k chord; it is the active item on its list and detail pages", async () => {
    const item = allNavItems.find((entry) => entry.id === "knowledge")!;
    expect(item).toMatchObject({ href: "/knowledge", group: "primitives", gKey: "k", label: "Knowledge" });
    const group = allNavItems.filter((entry) => entry.group === "primitives").map((entry) => entry.id);
    expect(group).toEqual(["agents", "architect", "workflows", "tools", "mcp", "processors", "knowledge", "webhooks", "external-access"]);
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-document-row");
    expect(sidebarLink()).toHaveAttribute("href", "/knowledge");
    expect(sidebarLink()).toHaveAttribute("aria-current", "page");
    expect(document.querySelectorAll('[data-testid="sidebar"] [aria-current="page"][data-nav-id]')).toHaveLength(1);
    cleanup();
    nextRouter.reset(["/knowledge"]);
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-base-row");
    expect(sidebarLink()).toHaveAttribute("aria-current", "page");
  });

  it("g k goes to /knowledge from the knowledge base page; it is ignored while typing in a field", async () => {
    nextRouter.reset(["/knowledge/kb1?tab=search"]);
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const query = await screen.findByTestId("knowledge-search-query");
    query.focus();
    fireEvent.keyDown(query, { key: "g", bubbles: true });
    fireEvent.keyDown(query, { key: "k", bubbles: true });
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/knowledge/kb1?tab=search");
    (query as HTMLElement).blur();
    act(() => pressKeys("g", "k"));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/knowledge");
  });

  it("c on /knowledge opens New knowledge base; it is typed as text inside the dialog's fields and does not open a second one", async () => {
    nextRouter.reset(["/knowledge"]);
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-base-row");
    act(() => pressKey("c"));
    const dialog = await screen.findByTestId("knowledge-create-dialog");
    const name = within(dialog).getByTestId("knowledge-create-name");
    fireEvent.keyDown(name, { key: "c", bubbles: true });
    fireEvent.change(name, { target: { value: "c" } });
    expect(screen.getAllByTestId("knowledge-create-dialog")).toHaveLength(1);
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByTestId("knowledge-create-dialog")).toBeNull());
    act(() => pressKey("c"));
    expect(await screen.findByTestId("knowledge-create-dialog")).toBeInTheDocument();
    expect(within(screen.getByTestId("knowledge-create-dialog")).getByTestId("knowledge-create-name")).toHaveValue("");
  });

  it("the Knowledge scope of the palette lists the knowledge bases from the API; Enter opens one", async () => {
    nextRouter.reset(["/knowledge"]);
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-base-row");
    act(() => pressKey("k", { metaKey: true }));
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    fireEvent.click(within(dialog).getByRole("radio", { name: "Knowledge" }));
    await waitFor(() => expect(dialog.querySelectorAll('[data-palette-entity^="knowledge:"]')).toHaveLength(2));
    expect(dialog.querySelectorAll("[data-palette-page], [data-palette-command]")).toHaveLength(0);
    const input = within(dialog).getByTestId("command-palette-input");
    fireEvent.change(input, { target: { value: "hand" } });
    expect(dialog.querySelectorAll('[data-palette-entity^="knowledge:"]')).toHaveLength(1);
    fireEvent.keyDown(input, { key: "Enter" });
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/knowledge/kb2");
  });

  it("a failing knowledge list is that source's error only; the Pages scope still offers the Knowledge page", async () => {
    nextRouter.reset(["/knowledge"]);
    on("GET", /^\/knowledge-bases$/, () => json(500, { detail: "kb down" }));
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    await screen.findByTestId("datalist-error");
    act(() => pressKey("k", { metaKey: true }));
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    await waitFor(() => expect(screen.queryByTestId("command-palette-loading")).toBeNull());
    expect(await screen.findByTestId("command-palette-error-knowledge")).toBeInTheDocument();
    expect(dialog.querySelector('[data-palette-page="knowledge"]')).not.toBeNull();
  });

  it("the shortcut help lists g k for Knowledge", async () => {
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-base-row");
    act(() => pressKey("?"));
    const help = await screen.findByRole("dialog");
    expect(help).toHaveTextContent("Knowledge");
  });
});

// ---------------------------------------------------------------------------------------------
// Accessibility in both themes (one h1 per page, axe on every surface)

const oneH1 = (name: string) => {
  const h1s = screen.getAllByRole("heading", { level: 1 });
  expect(h1s.map((h) => h.textContent)).toEqual([name]);
};

describe.each(["light", "dark"] as const)("accessibility (%s theme)", (theme) => {
  let media: MatchMediaControl;
  beforeEach(() => {
    media = mockMatchMedia(true);
    document.documentElement.lang = "en";
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    document.documentElement.classList.toggle("dark", theme === "dark");
    on("GET", /^\/knowledge-bases\/kb1\/documents$/, () => json(200, { items: [
      doc("install.md", { metadata: { lang: "en" } }),
      doc("billing.md", { status: "failed", error: "embedding_failed: provider down", chunk_count: 0, active_version: null }),
      doc("guide.md", { status: "ingesting", chunk_count: 0, last_run_id: "run-live-g" }),
    ], next_after: null }));
    on("GET", /^\/knowledge-bases\/kb1\/documents\/install\.md$/, () => json(200, doc("install.md", { metadata: { lang: "en" }, source_url: "https://docs.example.com/install" })));
  });
  afterEach(() => {
    media.restore();
    window.localStorage.removeItem(THEME_STORAGE_KEY);
  });

  const check = async (rules?: typeof PAGE_RULES) => {
    expect(document.documentElement.classList.contains("dark")).toBe(theme === "dark");
    await expectNoAxeViolations(document, rules ? { rules } : {});
  };

  it("the list: one h1, no violations; the create dialog open", async () => {
    nextRouter.reset(["/knowledge"]);
    render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-base-row");
    oneH1("Knowledge");
    await check(PAGE_RULES);
    fireEvent.click(screen.getByTestId("knowledge-new"));
    await screen.findByTestId("knowledge-create-dialog");
    await waitFor(() => expect(screen.getByTestId("knowledge-embedding-model")).not.toBeDisabled());
    await check();
  }, AXE_TIMEOUT_MS);

  it("the Documents tab with a failed and an ingesting document: one h1, no violations", async () => {
    await renderDetail();
    await waitFor(() => expect(screen.getAllByTestId("knowledge-document-row")).toHaveLength(3));
    oneH1("Product docs");
    await check(PAGE_RULES);
  }, AXE_TIMEOUT_MS);

  it("the upload Sheet with a ready and a refused file, and its paste form", async () => {
    const sheet = await openUpload();
    drop(sheet, [fileOf("ok.md"), fileOf("scan.docx")]);
    await check();
    fireEvent.mouseDown(within(sheet).getByTestId("knowledge-upload-tab-paste"));
    await screen.findByTestId("knowledge-upload-paste");
    await check();
  }, AXE_TIMEOUT_MS);

  it("the document Sheet over the page: still one h1, no violations", async () => {
    nextRouter.reset(["/knowledge/kb1?doc=install.md"]);
    render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    const sheet = await screen.findByTestId("knowledge-document-sheet");
    await within(sheet).findByTestId("knowledge-document-versions");
    await check();
  }, AXE_TIMEOUT_MS);

  it("the reindex dialog with an over-budget estimate; the delete dialog with the API's reference list", async () => {
    on("POST", /\/reindex$/, () => json(200, estimate({ approx_tokens: 3_000_000, exceeds_budget: true })));
    on("DELETE", /^\/knowledge-bases\/kb1$/, () => json(409, { detail: "attached", agents: [{ agent_id: "a1", agent_name: "Support", version: 3, archived: false }] }));
    const dialog = await openReindex();
    await dialog.findByTestId("knowledge-reindex-over-budget");
    await check();
    fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByTestId("knowledge-reindex-dialog")).toBeNull());
    fireEvent.click(screen.getByTestId("knowledge-delete"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Delete knowledge base" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Delete knowledge base" }));
    await screen.findByRole("button", { name: "Delete anyway" });
    await check();
  }, AXE_TIMEOUT_MS);

  it("the Search tab with results and the Ingest runs tab: one h1, no violations", async () => {
    on("POST", /\/search$/, () => searchOk([hit(1, { url: "https://docs.example.com/a", heading_path: ["A", "B"], metadata: { lang: "en" } }), hit(2)]));
    on("GET", /^\/runs$/, () => json(200, { items: [ingestRun("run-a", { reindex: 2 })], next_before: null }));
    await search();
    await screen.findAllByTestId("knowledge-search-result");
    oneH1("Product docs");
    await check(PAGE_RULES);
    cleanup();
    await renderDetail("runs");
    await screen.findAllByTestId("knowledge-run-row");
    oneH1("Product docs");
    await check(PAGE_RULES);
  }, AXE_TIMEOUT_MS);

  it("a failed reindex banner and a rendered answer with citation links", async () => {
    on("GET", /^\/knowledge-bases\/kb1$/, () => json(200, detail({ status: "reindex_failed", pending_generation: 2, pending_embedding_model: "litellm:qwen", error: "embedding_failed: down" })));
    await renderDetail();
    await screen.findByTestId("knowledge-reindex-banner");
    await check(PAGE_RULES);
    cleanup();
    const view = say("Install [1]; billing [2]; refunds [3].");
    expect(cites()).toHaveLength(3);
    await expectNoAxeViolations(view.container);
  }, AXE_TIMEOUT_MS);
});

// ---------------------------------------------------------------------------------------------
// Phone width, by classes (jsdom does no layout)

describe("phone layout", () => {
  const classes = (el: Element | null) => (el?.className ?? "").toString().split(/\s+/);
  // An unprefixed fixed width only: a breakpoint-prefixed one (`md:w-[480px]`) does not apply on a phone.
  const noFixedWidths = (root: Element) => expect(root.innerHTML).not.toMatch(/(?<![\w:-])(min-)?w-\[\d{3,}px\]/);
  let innerWidth: number;
  beforeEach(() => {
    innerWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { value: 375, configurable: true, writable: true });
  });
  afterEach(() => {
    Object.defineProperty(window, "innerWidth", { value: innerWidth, configurable: true, writable: true });
  });

  it("the list hides model, sections and cost columns below their breakpoints, truncates names, and hides the C hint", async () => {
    nextRouter.reset(["/knowledge"]);
    const view = render(<KeyboardHarness><KnowledgePage /></KeyboardHarness>);
    const row = (await screen.findAllByTestId("knowledge-base-row")).find((r) => within(r).queryByText("Product docs"))!;
    const hidden = (header: string, cellText: string | RegExp, breakpoint: string) => {
      expect(classes(screen.getByRole("columnheader", { name: header }))).toEqual(expect.arrayContaining(["hidden", breakpoint]));
      expect(classes(within(row).getByText(cellText).closest("td"))).toEqual(expect.arrayContaining(["hidden", breakpoint]));
    };
    hidden("Embedding model", "test:hash", "md:table-cell");
    hidden("Sections", /^9$/, "sm:table-cell");
    expect(classes(screen.getByRole("columnheader", { name: /Ingest cost/ }))).toEqual(expect.arrayContaining(["hidden", "lg:table-cell"]));
    expect(classes(within(row).getByText("Product docs").closest("span"))).toContain("truncate");
    expect(classes(within(screen.getByTestId("knowledge-new")).getByText("C"))).toContain("hidden");
    noFixedWidths(view.container);
  });

  it("the detail page: the summary wraps in an auto-fit grid, Reindex and Delete keep only an icon and a screen-reader label, secondary columns hide", async () => {
    const view = render(<KeyboardHarness><KnowledgeBasePage /></KeyboardHarness>);
    await screen.findAllByTestId("knowledge-document-row");
    expect(classes(screen.getByTestId("knowledge-summary"))).toEqual(expect.arrayContaining(["grid", "min-w-0"]));
    expect(screen.getByTestId("knowledge-summary").className).toContain("auto-fit");
    for (const id of ["knowledge-reindex", "knowledge-delete"]) {
      const visible = within(screen.getByTestId(id)).getAllByText(/Reindex|Delete/).find((el) => classes(el).includes("hidden"))!;
      expect(classes(visible)).toContain("sm:inline");
      expect(within(screen.getByTestId(id)).getAllByText(/Reindex|Delete/).some((el) => classes(el).includes("sr-only"))).toBe(true);
    }
    expect(classes(screen.getByRole("columnheader", { name: "Version" }))).toEqual(expect.arrayContaining(["hidden", "md:table-cell"]));
    expect(classes(screen.getByRole("columnheader", { name: "Updated" }))).toEqual(expect.arrayContaining(["hidden", "lg:table-cell"]));
    const title = within(screen.getAllByTestId("knowledge-document-row")[0]).getByText("install");
    expect(classes(title)).toContain("truncate");
    noFixedWidths(view.container);
  });

  it("the Sheets, the search form and results fit: single-column forms, wrapping URLs, shrinkable children", async () => {
    on("POST", /\/search$/, () => searchOk([hit(1, { url: "https://docs.example.com/" + "a".repeat(120), heading_path: ["A".repeat(80)] })]));
    await search();
    const result = await screen.findByTestId("knowledge-search-result");
    expect(classes(within(result).getByTestId("knowledge-search-url").parentElement)).toEqual(expect.arrayContaining(["break-all", "min-w-0"]));
    expect(classes(screen.getByTestId("knowledge-search-form").querySelector(".grid"))).toContain("grid-cols-[repeat(auto-fit,minmax(min(100%,10rem),1fr))]");
    cleanup();
    nextRouter.reset(["/knowledge/kb1"]);
    const sheet = await openUpload();
    expect(classes(within(sheet).getByTestId("knowledge-upload-dropzone"))).toContain("min-w-0");
    fireEvent.mouseDown(within(sheet).getByTestId("knowledge-upload-tab-paste"));
    const paste = await screen.findByTestId("knowledge-upload-paste");
    for (const grid of Array.from(paste.querySelectorAll(".grid"))) expect(classes(grid)).toContain("sm:grid-cols-2");
    noFixedWidths(document.body);
  });
});
