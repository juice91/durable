// RAG P1.A5 acceptance (tester): the Studio journey for native formats, beyond nativeFormats.test.tsx:
// API refusals 413/415/422, cap boundaries, the double-click guard, warnings, the bulk dialog in the
// Documents tab, Advanced extraction "only when set", phone layout by classes, and axe.
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

import { nextRouter } from "@/test/nextRouterMock";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { checkUploadFile, documentPutProblem, uploadCapsText, UPLOAD_ACCEPT } from "@/lib/knowledge";
import type { KnowledgeDocument } from "@/lib/knowledge";
import { DocumentSheet } from "./DocumentSheet";
import { DocumentsTab } from "./DocumentsTab";
import { KnowledgeBaseDialog } from "./KnowledgeBaseDialog";
import { UploadSheet } from "./UploadSheet";

const T0 = "2026-11-15T10:00:00Z";
type Handler = (url: URL, body: unknown) => { status?: number; body?: unknown };
let table: Array<[string, RegExp, Handler]>;
let requests: Array<{ method: string; path: string; body: unknown }>;
const on = (method: string, path: RegExp, handler: Handler) => table.unshift([method, path, handler]);
const sent = (method: string, path: RegExp) => requests.filter((r) => r.method === method && path.test(r.path));

function doc(key: string, extra: Partial<KnowledgeDocument> = {}): KnowledgeDocument {
  return {
    id: `d-${key}`, tenant_id: "t", kb_id: "kb1", document_key: key, title: key, source_url: null, mime_type: "application/pdf",
    metadata: {}, content_hash: "h", active_version: 1, latest_version: 1, status: "ready", error: null, chunk_count: 12,
    last_run_id: "run-1", created_at: T0, updated_at: T0, content: null, content_encoding: "base64", error_code: null, error_reason: null,
    ...extra,
  };
}

beforeEach(() => {
  nextRouter.reset(["/knowledge/kb1"]);
  table = [];
  requests = [];
  document.title = "Knowledge · Studio";
  document.documentElement.lang = "en";
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = new URL(input);
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const method = (init.method ?? "GET").toUpperCase();
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ method, path, body });
    for (const [m, re, handler] of table) {
      if (m === method && re.test(path)) {
        const r = handler(url, body);
        return new Response(JSON.stringify(r.body ?? {}), { status: r.status ?? 200, headers: { "content-type": "application/json" } });
      }
    }
    return new Response(JSON.stringify({ items: [] }), { status: 200, headers: { "content-type": "application/json" } });
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const classes = (el: Element | null) => (el?.className ?? "").toString().split(/\s+/);
const fileOfSize = (name: string, size: number) => {
  const f = new File(["x"], name);
  Object.defineProperty(f, "size", { value: size });
  return f;
};
const MiB = 1024 * 1024;

describe("caps and accept list (pure)", () => {
  it("the accept list names every native extension and application/pdf", () => {
    const parts = UPLOAD_ACCEPT.split(",");
    for (const x of [".md", ".txt", ".html", ".rst", ".pdf", ".yaml", ".yml", ".json", "application/pdf"]) expect(parts).toContain(x);
    expect(uploadCapsText()).toContain("PDF up to 25 MiB");
  });

  it("a file exactly at its cap passes and one byte over is refused, per type", () => {
    expect(checkUploadFile(fileOfSize("a.md", 5 * MiB)).ok).toBe(true);
    expect(checkUploadFile(fileOfSize("a.md", 5 * MiB + 1))).toMatchObject({ ok: false, reason: "The file is larger than 5 MiB." });
    expect(checkUploadFile(fileOfSize("a.json", 2 * MiB)).ok).toBe(true);
    expect(checkUploadFile(fileOfSize("a.yml", 2 * MiB + 1))).toMatchObject({ ok: false, reason: "The file is larger than 2 MiB." });
    expect(checkUploadFile(fileOfSize("a.pdf", 25 * MiB)).ok).toBe(true);
    expect(checkUploadFile(fileOfSize("a.pdf", 25 * MiB + 1))).toMatchObject({ ok: false, reason: "The file is larger than 25 MiB." });
    expect(checkUploadFile(new File(["x"], "a.docx"))).toMatchObject({ ok: false, reason: expect.stringContaining("Unsupported file type") });
  });

  it("a PDF's base64 body is bounded by its decoded size, not the text cap", () => {
    const big = "A".repeat(Math.ceil((20 * MiB * 4) / 3));
    expect(documentPutProblem("a.pdf", { mime_type: "application/pdf", content_base64: big })).toBeNull();
    expect(documentPutProblem("a.md", { mime_type: "text/markdown", content: "x".repeat(5 * MiB + 1) })).toMatch(/larger than 5 MiB/);
  });
});

describe("UploadSheet: the API's refusals show on the row", () => {
  async function failWith(status: number, detail: string, file: File) {
    on("PUT", /\/documents\//, () => ({ status, body: { detail } }));
    render(<UploadSheet open onOpenChange={() => undefined} kbId="kb1" kbName="TI" onUploaded={() => undefined} />);
    const sheet = screen.getByTestId("knowledge-upload-sheet");
    fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), { dataTransfer: { files: [file] } });
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    await waitFor(() => expect(within(sheet).getByTestId("knowledge-upload-file").getAttribute("data-status")).toBe("failed"));
    return within(sheet).getByTestId("knowledge-upload-file");
  }

  it.each([
    [413, "The file is larger than 25 MiB.", "big.pdf"],
    [415, "unsupported media type: application/octet-stream", "x.json"],
    [422, "the PDF is encrypted with a password", "locked.pdf"],
  ])("%i shows the API's message", async (status, detail, name) => {
    // A text file with a binary signature is now refused before sending (browser check C), so the
    // JSON case sends real JSON to reach the API's 415.
    const row = await failWith(status, detail, new File([name.endsWith(".pdf") ? "%PDF-1.7" : "{}"], name));
    expect(row).toHaveTextContent(detail);
  });
});

describe("DocumentSheet: pages, warnings and the Re-extract guard", () => {
  const extraction = { extractor: "pdf", extractor_version: 2, current_version: 2, doc_version: 1, run_id: "run-1", sections: 10, chars: 5000, pages: 1, image_only_pages: [4], warnings: 2 };
  const open = () => render(<DocumentSheet kbId="kb1" documentKey="m.pdf" listed={null} progress={null} onOpenChange={() => undefined} onDeleted={() => undefined} onLoaded={() => undefined} onReextracted={() => undefined} />);

  it("shows the singular page, the warning count and the singular image-only text; no outdated note when current", async () => {
    on("GET", /documents\/m\.pdf$/, () => ({ body: doc("m.pdf", { extraction }) }));
    open();
    const sheet = screen.getByTestId("knowledge-document-sheet");
    expect(await within(sheet).findByTestId("knowledge-document-extractor")).toHaveTextContent("PDF extractor v2 · 1 page · 10 sections · 2 warnings");
    expect(within(sheet).getByTestId("knowledge-document-image-only")).toHaveTextContent("1 page has no text: scanned?");
    expect(within(sheet).queryByTestId("knowledge-document-extractor-outdated")).toBeNull();
  });

  it("a double click on Re-extract sends one request with one client run_id", async () => {
    on("GET", /documents\/m\.pdf$/, () => ({ body: doc("m.pdf", { extraction }) }));
    on("POST", /reextract$/, () => ({ status: 202, body: { run_id: "r2", kind: "kb_ingest", knowledge_base_id: "kb1", status: "queued", document: doc("m.pdf", { latest_version: 2, status: "pending" }) } }));
    open();
    const sheet = screen.getByTestId("knowledge-document-sheet");
    await within(sheet).findByTestId("knowledge-document-extractor");
    fireEvent.click(within(sheet).getByTestId("knowledge-document-reextract"));
    const dialog = await screen.findByRole("dialog", { name: "Re-extract m.pdf?" });
    const button = within(dialog).getByRole("button", { name: "Re-extract" });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Re-extract m.pdf?" })).toBeNull());
    const posts = sent("POST", /reextract$/);
    expect(posts).toHaveLength(1);
    expect((posts[0].body as { run_id: string }).run_id).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("cancelling the confirm sends nothing", async () => {
    on("GET", /documents\/m\.pdf$/, () => ({ body: doc("m.pdf", { extraction }) }));
    open();
    const sheet = screen.getByTestId("knowledge-document-sheet");
    await within(sheet).findByTestId("knowledge-document-extractor");
    fireEvent.click(within(sheet).getByTestId("knowledge-document-reextract"));
    const dialog = await screen.findByRole("dialog", { name: "Re-extract m.pdf?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(sent("POST", /reextract$/)).toHaveLength(0);
  });

  it("phone width: no fixed pixel widths in the Sheet", async () => {
    on("GET", /documents\/m\.pdf$/, () => ({ body: doc("m.pdf", { extraction }) }));
    open();
    const sheet = screen.getByTestId("knowledge-document-sheet");
    await within(sheet).findByTestId("knowledge-document-extractor");
    expect(sheet.innerHTML).not.toMatch(/(?<![\w:-])(min-)?w-\[\d{3,}px\]/);
    expect(classes(within(sheet).getByTestId("knowledge-document-extractor")).join(" ")).not.toMatch(/whitespace-nowrap/);
  });
});

describe("DocumentsTab: Format column and the bulk Re-extract outdated", () => {
  const docs = (list: KnowledgeDocument[]) => ({ documents: list, loading: false, loadingMore: false, error: null, hasMore: false, reload: vi.fn(), loadMore: vi.fn(), upsert: vi.fn(), remove: vi.fn(), refresh: vi.fn() });
  const dry = (selected: number) => ({ knowledge_base_id: "kb1", dry_run: true, chunker_version: 1, chunker_version_changed: false, selected, started: 0, failed: 0, documents: [] });

  it("shows each document's format, opens the dialog, previews, starts, and reports", async () => {
    on("POST", /^\/knowledge-bases\/kb1\/reextract$/, (_u, body) => ({ status: (body as { dry_run?: boolean }).dry_run ? 200 : 202, body: (body as { dry_run?: boolean }).dry_run ? dry(2) : { ...dry(2), dry_run: false, started: 2 } }));
    const onReextracted = vi.fn();
    const state = docs([doc("a.pdf"), doc("b.md", { mime_type: "text/markdown", content_encoding: null })]);
    render(<DocumentsTab kbId="kb1" docs={state} progress={{}} openDocument={null} onOpenDocument={() => undefined} onUpload={() => undefined} chunkerVersion={2} onReextracted={onReextracted} />);
    expect(screen.getAllByTestId("knowledge-document-format-cell").map((c) => c.textContent)).toEqual(expect.arrayContaining([expect.stringMatching(/PDF/), expect.stringMatching(/Markdown/i)]));
    fireEvent.click(screen.getByTestId("knowledge-documents-reextract"));
    expect(await screen.findByTestId("knowledge-reextract-count")).toHaveTextContent("2 documents will be re-extracted.");
    fireEvent.click(screen.getByTestId("knowledge-reextract-start"));
    await waitFor(() => expect(onReextracted).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId("knowledge-documents-info")).toHaveTextContent("Re-extract started for 2 documents.");
    expect(sent("POST", /reextract$/).map((r) => r.body)).toEqual([{ only_outdated: true, dry_run: true }, { only_outdated: true }]);
  });

  it("chunker v2: the dry run previews the whole base, and Start sends chunker_version 2 without dry_run", async () => {
    on("POST", /^\/knowledge-bases\/kb1\/reextract$/, (_u, body) => ({ body: (body as { dry_run?: boolean }).dry_run ? dry(7) : { ...dry(7), dry_run: false, started: 7, chunker_version_changed: true } }));
    render(<DocumentsTab kbId="kb1" docs={docs([doc("a.pdf")])} progress={{}} openDocument={null} onOpenDocument={() => undefined} onUpload={() => undefined} chunkerVersion={1} />);
    fireEvent.click(screen.getByTestId("knowledge-documents-reextract"));
    await screen.findByTestId("knowledge-reextract-count");
    fireEvent.click(screen.getByTestId("knowledge-reextract-chunker-v2"));
    await waitFor(() => expect(screen.getByTestId("knowledge-reextract-count")).toHaveTextContent("7 documents will be re-extracted."));
    fireEvent.click(screen.getByTestId("knowledge-reextract-start"));
    expect(await screen.findByTestId("knowledge-documents-info")).toHaveTextContent("The knowledge base now uses chunker v2.");
    expect(sent("POST", /reextract$/).at(-1)!.body).toEqual({ chunker_version: 2 });
  });

  it("a failed dry run shows an error and keeps Start disabled", async () => {
    on("POST", /^\/knowledge-bases\/kb1\/reextract$/, () => ({ status: 500, body: { detail: "preview failed" } }));
    render(<DocumentsTab kbId="kb1" docs={docs([doc("a.pdf")])} progress={{}} openDocument={null} onOpenDocument={() => undefined} onUpload={() => undefined} />);
    fireEvent.click(screen.getByTestId("knowledge-documents-reextract"));
    const preview = screen.getByTestId("knowledge-reextract-preview");
    await waitFor(() => expect(preview).toHaveAttribute("data-state", "error"));
    expect(within(preview).getByRole("alert")).toHaveTextContent("preview failed");
    expect(screen.getByTestId("knowledge-reextract-start")).toBeDisabled();
  });
});

describe("KnowledgeBaseDialog: Advanced extraction is saved only when set", () => {
  beforeEach(() => {
    on("GET", /^\/embedding-models$/, () => ({ body: { items: [{ id: "test:hash", provider: "test", name: "hash", dimensions: 1536, wildcard: false }] } }));
    on("POST", /^\/knowledge-bases$/, (_u, body) => ({ status: 201, body: { id: "kb9", ...(body as object) } }));
  });
  async function fill() {
    render(<KnowledgeBaseDialog onCreated={() => undefined} onClose={() => undefined} />);
    fireEvent.change(screen.getByTestId("knowledge-create-name"), { target: { value: "K" } });
    const picker = await screen.findByTestId("knowledge-embedding-model");
    await waitFor(() => expect(picker).not.toBeDisabled());
    fireEvent.change(picker, { target: { value: "test:hash" } });
  }

  it("opening Advanced without setting anything still sends no chunking", async () => {
    await fill();
    fireEvent.click(screen.getByTestId("knowledge-create-advanced"));
    await screen.findByTestId("knowledge-create-pdf-headings");
    fireEvent.click(screen.getByTestId("knowledge-create-submit"));
    await waitFor(() => expect(sent("POST", /^\/knowledge-bases$/)).toHaveLength(1));
    expect(sent("POST", /^\/knowledge-bases$/)[0].body).not.toHaveProperty("chunking");
  });

  it("sends only pdf_headings when only it is set", async () => {
    await fill();
    fireEvent.click(screen.getByTestId("knowledge-create-advanced"));
    fireEvent.change(await screen.findByTestId("knowledge-create-pdf-headings"), { target: { value: "outline" } });
    fireEvent.click(screen.getByTestId("knowledge-create-submit"));
    await waitFor(() => expect(sent("POST", /^\/knowledge-bases$/)).toHaveLength(1));
    expect((sent("POST", /^\/knowledge-bases$/)[0].body as { chunking: unknown }).chunking).toEqual({ extract: { pdf_headings: "outline" } });
  });
});

describe("axe and phone layout", () => {
  it("the upload Sheet has no axe violations and no fixed widths", async () => {
    render(<UploadSheet open onOpenChange={() => undefined} kbId="kb1" kbName="TI" onUploaded={() => undefined} />);
    const sheet = screen.getByTestId("knowledge-upload-sheet");
    fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), { dataTransfer: { files: [new File(["x"], "a.pdf"), fileOfSize("b.docx", 10)] } });
    expect(sheet.innerHTML).not.toMatch(/(?<![\w:-])(min-)?w-\[\d{3,}px\]/);
    await expectNoAxeViolations();
  }, AXE_TIMEOUT_MS);

  it("the Documents tab with the Format column has no axe violations", async () => {
    const state = { documents: [doc("a.pdf")], loading: false, loadingMore: false, error: null, hasMore: false, reload: vi.fn(), loadMore: vi.fn(), upsert: vi.fn(), remove: vi.fn(), refresh: vi.fn() };
    const view = render(<DocumentsTab kbId="kb1" docs={state} progress={{}} openDocument={null} onOpenDocument={() => undefined} onUpload={() => undefined} />);
    expect(view.container.innerHTML).not.toMatch(/(?<![\w:-])(min-)?w-\[\d{3,}px\]/);
    await expectNoAxeViolations(view.container);
  }, AXE_TIMEOUT_MS);
});
