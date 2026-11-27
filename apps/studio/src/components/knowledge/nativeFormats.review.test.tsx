// RAG P1.A5 review fixes: PDFs are read with FileReader.readAsDataURL (no main-thread base64) and
// sized arithmetically, with a "Reading file…" state; the 25 MiB cap boundary with real padded
// base64; a bulk re-extract sends one request however fast it is clicked; a deep-linked document
// re-reads itself until its re-extract ends; unsafe source URLs with a page never become links.
import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

import { nextRouter } from "@/test/nextRouterMock";
import { citationHref, parseSources } from "@/components/chat/citations";
import {
  bytesToBase64,
  documentPutBase64Bytes,
  documentPutBytes,
  documentPutJson,
  documentPutProblem,
  readFileAsBase64,
  readUploadContent,
  type KnowledgeDocument,
} from "@/lib/knowledge";
import { routes } from "@/lib/routes";
import { DocumentSheet } from "./DocumentSheet";
import { ReextractDialog } from "./ReextractDialog";
import { UploadSheet } from "./UploadSheet";

const MiB = 1024 * 1024;
const T0 = "2026-11-15T10:00:00Z";

type Handler = (url: URL, body: unknown) => { status?: number; body?: unknown } | Promise<{ status?: number; body?: unknown }>;
let routesTable: Array<[string, RegExp, Handler]>;
let requests: Array<{ method: string; path: string; body: unknown }>;
const on = (method: string, path: RegExp, handler: Handler) => routesTable.unshift([method, path, handler]);
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
  routesTable = [];
  requests = [];
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = new URL(input);
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const method = (init.method ?? "GET").toUpperCase();
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ method, path, body });
    for (const [m, re, handler] of routesTable) {
      if (m === method && re.test(path)) {
        const result = await handler(url, body);
        return new Response(JSON.stringify(result.body ?? {}), { status: result.status ?? 200, headers: { "content-type": "application/json" } });
      }
    }
    return new Response(JSON.stringify({ items: [] }), { status: 200, headers: { "content-type": "application/json" } });
  }));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("PDF reading off the main thread (review MAJOR 1)", () => {
  it("readFileAsBase64 uses FileReader.readAsDataURL and strips the data: prefix", async () => {
    const dataUrl = vi.spyOn(FileReader.prototype, "readAsDataURL");
    const buffer = vi.spyOn(FileReader.prototype, "readAsArrayBuffer");
    const bytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0xff, 0x00, 0x80, 0x7f]);
    const blob = new Blob([bytes], { type: "application/pdf" });
    expect(await readFileAsBase64(blob)).toBe(bytesToBase64(bytes));
    expect(await readUploadContent(new Blob([bytes]), { binary: true })).toEqual({ content_base64: bytesToBase64(bytes) });
    expect(dataUrl).toHaveBeenCalledTimes(2);
    expect(buffer).not.toHaveBeenCalled();
  });

  it("documentPutBase64Bytes equals the serialised body's size, without serialising the content", () => {
    const body = { title: "Manuál ✓", content_base64: bytesToBase64(new Uint8Array(3000).map((_, i) => i % 256)), mime_type: "application/pdf", metadata: { device: "am62x" } };
    expect(documentPutBase64Bytes("trm.pdf", body)).toBe(documentPutBytes("trm.pdf", body));
    expect(documentPutBase64Bytes("trm.pdf", body)).toBe(new TextEncoder().encode(documentPutJson("trm.pdf", body)).length);
    // The size check of a base64 upload never stringifies its content.
    const stringify = vi.spyOn(JSON, "stringify");
    documentPutProblem("trm.pdf", body);
    for (const [value] of stringify.mock.calls) expect((value as { content_base64?: string }).content_base64 ?? "").toBe("");
  });

  it("a file shows Reading file… while it is read, then Uploading…", async () => {
    let finishRead: () => void = () => undefined;
    const original = FileReader.prototype.readAsDataURL;
    const dataUrl = vi.spyOn(FileReader.prototype, "readAsDataURL").mockImplementation(function (this: FileReader, blob: Blob) {
      finishRead = () => original.call(this, blob);
    });
    let answer: (value: { status: number; body: unknown }) => void = () => undefined;
    on("PUT", /\/documents\//, () => new Promise((resolve) => { answer = resolve; }));
    render(<UploadSheet open onOpenChange={() => undefined} kbId="kb1" kbName="Acme SDK" onUploaded={() => undefined} />);
    const sheet = screen.getByTestId("knowledge-upload-sheet");
    fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), { dataTransfer: { files: [new File(["%PDF-1.7"], "trm.pdf")] } });
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    const row = within(sheet).getByTestId("knowledge-upload-file");
    await waitFor(() => expect(row).toHaveAttribute("data-status", "reading"));
    expect(row).toHaveTextContent("Reading file…");
    // The 1 KiB %PDF- check reads first; the base64 read starts after it.
    await waitFor(() => expect(dataUrl).toHaveBeenCalled());
    act(() => finishRead());
    await waitFor(() => expect(row).toHaveAttribute("data-status", "uploading"));
    expect(sent("PUT", /\/documents\//)).toHaveLength(1);
    await act(async () => answer({ status: 202, body: { unchanged: false, created: true, document: doc("trm.pdf"), run_id: "r" } }));
    await waitFor(() => expect(row).toHaveAttribute("data-status", "started"));
  });
});

describe("the 25 MiB cap with real padded base64 (review MINOR 3)", () => {
  it("a PDF of exactly 25 MiB passes; one byte more is refused", () => {
    // Node's native encoder: the same standard padded base64 as FileReader (bytesToBase64 is checked against it).
    const b64 = (n: number) => Buffer.alloc(n).toString("base64");
    expect(b64(4096)).toBe(bytesToBase64(new Uint8Array(4096)));
    const exact = b64(25 * MiB);
    expect(exact.endsWith("=")).toBe(true); // 25 MiB is not a multiple of 3: real padding
    expect(documentPutProblem("trm.pdf", { content_base64: exact, mime_type: "application/pdf" })).toBeNull();
    const over = b64(25 * MiB + 1);
    expect(documentPutProblem("trm.pdf", { content_base64: over, mime_type: "application/pdf" })).toBe("The file is larger than 25 MiB.");
  });
});

describe("bulk re-extract sends one request (review MINOR 2)", () => {
  const result = (selected: number, dryRun: boolean) => ({ knowledge_base_id: "kb1", dry_run: dryRun, chunker_version: 2, chunker_version_changed: false, selected, started: dryRun ? 0 : selected, failed: 0, documents: [] });

  it("two clicks in a row, and a click while the request is in flight, start it once", async () => {
    let release: () => void = () => undefined;
    on("POST", /^\/knowledge-bases\/kb1\/reextract$/, (_url, body) => {
      if ((body as { dry_run?: boolean }).dry_run) return { body: result(3, true) };
      return new Promise((resolve) => { release = () => resolve({ status: 202, body: result(3, false) }); });
    });
    const onStarted = vi.fn();
    render(<ReextractDialog kbId="kb1" chunkerVersion={2} onStarted={onStarted} onClose={() => undefined} />);
    await screen.findByTestId("knowledge-reextract-count");
    const start = screen.getByTestId("knowledge-reextract-start");
    fireEvent.click(start);
    fireEvent.click(start);
    await waitFor(() => expect(start).toBeDisabled());
    fireEvent.click(start);
    const starts = () => sent("POST", /reextract$/).filter((r) => !(r.body as { dry_run?: boolean }).dry_run);
    expect(starts()).toHaveLength(1);
    await act(async () => release());
    await waitFor(() => expect(onStarted).toHaveBeenCalledTimes(1));
    expect(starts()).toHaveLength(1);
  });

  it("after a failed start, the count is asked again and Start waits for it", async () => {
    let dryRuns = 0;
    let finishRecount: () => void = () => undefined;
    on("POST", /^\/knowledge-bases\/kb1\/reextract$/, (_url, body) => {
      if (!(body as { dry_run?: boolean }).dry_run) return { status: 502, body: { detail: "bad gateway" } };
      dryRuns += 1;
      if (dryRuns === 1) return { body: result(3, true) };
      return new Promise((resolve) => { finishRecount = () => resolve({ body: result(1, true) }); });
    });
    render(<ReextractDialog kbId="kb1" chunkerVersion={2} onStarted={() => undefined} onClose={() => undefined} />);
    await screen.findByTestId("knowledge-reextract-count");
    fireEvent.click(screen.getByTestId("knowledge-reextract-start"));
    expect(await screen.findByTestId("knowledge-reextract-error")).toHaveTextContent("bad gateway The count was refreshed");
    await waitFor(() => expect(dryRuns).toBe(2));
    expect(screen.getByTestId("knowledge-reextract-start")).toBeDisabled();
    await act(async () => finishRecount());
    expect(await screen.findByTestId("knowledge-reextract-count")).toHaveTextContent("1 document will be re-extracted.");
    expect(screen.getByTestId("knowledge-reextract-start")).not.toBeDisabled();
  });
});

describe("a deep-linked document re-reads itself until its re-extract ends (review MINOR 5)", () => {
  it("with no listed copy, the extraction line updates once the new version is active", async () => {
    let state: "old" | "ingesting" | "done" = "old";
    const v1 = { extractor: "pdf", extractor_version: 1, current_version: 2, pages: 10, sections: 20 };
    const v2 = { extractor: "pdf", extractor_version: 2, current_version: 2, pages: 10, sections: 22 };
    on("GET", /^\/knowledge-bases\/kb1\/documents\/trm\.pdf$/, () => ({
      body: state === "done"
        ? doc("trm.pdf", { active_version: 2, latest_version: 2, extraction: v2, updated_at: "2026-11-15T10:02:00Z" })
        : state === "ingesting"
          ? doc("trm.pdf", { latest_version: 2, status: "ingesting", last_run_id: "run-2", extraction: v1, updated_at: "2026-11-15T10:01:00Z" })
          : doc("trm.pdf", { extraction: v1 }),
    }));
    on("POST", /\/documents\/trm\.pdf\/reextract$/, () => {
      state = "ingesting";
      return { status: 202, body: { run_id: "run-2", kind: "kb_ingest", knowledge_base_id: "kb1", status: "queued", document: doc("trm.pdf", { latest_version: 2, status: "pending", last_run_id: "run-2", updated_at: "2026-11-15T10:01:00Z" }) } };
    });
    render(<DocumentSheet kbId="kb1" documentKey="trm.pdf" listed={null} progress={null} pollMs={20} onOpenChange={() => undefined} onDeleted={() => undefined} onLoaded={() => undefined} />);
    const sheet = screen.getByTestId("knowledge-document-sheet");
    expect(await within(sheet).findByTestId("knowledge-document-extractor")).toHaveTextContent("PDF extractor v1");
    expect(within(sheet).getByTestId("knowledge-document-extractor-outdated")).toBeInTheDocument();
    fireEvent.click(within(sheet).getByTestId("knowledge-document-reextract"));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Re-extract trm.pdf?" })).getByRole("button", { name: "Re-extract" }));
    await within(sheet).findByTestId("knowledge-document-reextract-note");
    await waitFor(() => expect(sent("GET", /documents\/trm\.pdf$/).length).toBeGreaterThanOrEqual(2));
    state = "done";
    await waitFor(() => expect(within(sheet).getByTestId("knowledge-document-extractor")).toHaveTextContent("PDF extractor v2 · 10 pages · 22 sections"));
    expect(within(sheet).queryByTestId("knowledge-document-extractor-outdated")).toBeNull();
    // Ready: the poll stops.
    const reads = sent("GET", /documents\/trm\.pdf$/).length;
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(sent("GET", /documents\/trm\.pdf$/).length).toBe(reads);
  });

  it("does not poll a document that is not ingesting, or one the list follows", async () => {
    on("GET", /^\/knowledge-bases\/kb1\/documents\/a\.pdf$/, () => ({ body: doc("a.pdf", { status: "ingesting" }) }));
    const { unmount } = render(<DocumentSheet kbId="kb1" documentKey="a.pdf" listed={doc("a.pdf", { status: "ingesting" })} progress={null} pollMs={20} onOpenChange={() => undefined} onDeleted={() => undefined} onLoaded={() => undefined} />);
    await within(screen.getByTestId("knowledge-document-sheet")).findByTestId("knowledge-document-format");
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(sent("GET", /documents\/a\.pdf$/)).toHaveLength(1);
    unmount();
  });
});

describe("unsafe source URLs with a page never become links (review MINOR 4)", () => {
  it("javascript: and data: URLs fall back to the Studio document, without #page", () => {
    const sources = parseSources([
      { n: 1, title: "A", url: "javascript:alert(1)#page=3", page: 3, kb_id: "kb1", document_key: "a.pdf" },
      { n: 2, title: "B", url: "data:text/html;base64,PHNjcmlwdD4=#page=4", page: 4, kb_id: "kb1", document_key: "b.pdf" },
      { n: 3, title: "C", url: "javascript:alert(1)", page: 5 },
    ]);
    expect(sources.map((s) => s.page)).toEqual([3, 4, 5]);
    expect(citationHref(sources[0])).toBe(routes.knowledgeBase("kb1", { document: "a.pdf" }));
    expect(citationHref(sources[1])).toBe(routes.knowledgeBase("kb1", { document: "b.pdf" }));
    expect(citationHref(sources[2])).toBeNull();
    for (const source of sources) expect(citationHref(source) ?? "").not.toContain("#page=");
  });
});
