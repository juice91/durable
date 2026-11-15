// RAG P1.A5: the knowledge components for the native formats: the upload Sheet (PDF as base64, the
// API's 415 message), the document Sheet (extraction line, image-only pages, Re-extract), the create
// dialog's "Advanced extraction", the bulk "Re-extract outdated", and the retrieve span's page.
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

import { nextRouter } from "@/test/nextRouterMock";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { SpanDetail } from "@/components/traces/SpanDetail";
import type { TraceSpan } from "@/lib/traces";
import type { KnowledgeDocument } from "@/lib/knowledge";
import { DocumentSheet } from "./DocumentSheet";
import { KnowledgeBaseDialog } from "./KnowledgeBaseDialog";
import { ReextractDialog, reextractRequest, reextractStartedText } from "./ReextractDialog";
import { UploadSheet } from "./UploadSheet";

const T0 = "2026-11-15T10:00:00Z";

type Handler = (url: URL, body: unknown) => { status?: number; body?: unknown };
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
        const result = handler(url, body);
        return new Response(JSON.stringify(result.body ?? {}), { status: result.status ?? 200, headers: { "content-type": "application/json" } });
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

const statuses = (sheet: HTMLElement) => within(sheet).getAllByTestId("knowledge-upload-file").map((row) => row.getAttribute("data-status"));

describe("UploadSheet", () => {
  function renderUpload() {
    const onUploaded = vi.fn();
    render(<UploadSheet open onOpenChange={() => undefined} kbId="kb1" kbName="TI SDK" onUploaded={onUploaded} />);
    return { sheet: screen.getByTestId("knowledge-upload-sheet"), onUploaded };
  }

  it("accepts .rst .pdf .yaml .yml .json and names the formats and caps", () => {
    const { sheet } = renderUpload();
    expect(within(sheet).getByTestId("knowledge-upload-formats")).toHaveTextContent(".md, .txt, .html, .rst, .pdf, .yaml, .yml or .json. Text up to 5 MiB, YAML and JSON up to 2 MiB, PDF up to 25 MiB.");
    const input = within(sheet).getByTestId("knowledge-upload-input");
    for (const ext of [".rst", ".pdf", ".yaml", ".yml", ".json"]) expect(input.getAttribute("accept")?.split(",")).toContain(ext);
    const big = (name: string, size: number) => {
      const file = new File(["x"], name);
      Object.defineProperty(file, "size", { value: size });
      return file;
    };
    fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), {
      dataTransfer: { files: [new File(["a"], "a.rst"), new File(["%PDF-1.7"], "b.pdf"), new File(["k: v"], "c.yaml"), new File(["k: v"], "d.yml"), new File(["{}"], "e.json"), big("f.json", 2 * 1024 * 1024 + 1), big("g.pdf", 26 * 1024 * 1024)] },
    });
    expect(statuses(sheet)).toEqual(["ready", "ready", "ready", "ready", "ready", "refused", "refused"]);
    expect(within(sheet).getAllByTestId("knowledge-upload-refused").map((el) => el.textContent)).toEqual(["The file is larger than 2 MiB.", "The file is larger than 25 MiB."]);
  });

  it("sends a PDF as content_base64 with application/pdf, and text formats as content", async () => {
    on("PUT", /\/documents\//, (url) => {
      const key = decodeURIComponent(url.pathname.split("/").pop()!);
      return { status: 202, body: { unchanged: false, created: true, document: doc(key), run_id: `run-${key}` } };
    });
    const { sheet, onUploaded } = renderUpload();
    const pdfBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x37, 0x0a, 0xe2, 0xe3]);
    fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), {
      dataTransfer: { files: [new File([pdfBytes], "trm.pdf"), new File(["Title\n=====\n"], "intro.rst")] },
    });
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    await waitFor(() => expect(statuses(sheet)).toEqual(["started", "started"]));
    const [pdf, rst] = sent("PUT", /\/documents\//).map((r) => r.body as Record<string, unknown>);
    expect(pdf).toEqual({ document_key: "trm.pdf", title: "trm", content_base64: btoa(String.fromCharCode(...pdfBytes)), mime_type: "application/pdf", metadata: null });
    expect(pdf).not.toHaveProperty("content");
    expect(rst).toEqual({ document_key: "intro.rst", title: "intro", content: "Title\n=====\n", mime_type: "text/x-rst", metadata: null });
    expect(onUploaded).toHaveBeenCalledTimes(2);
  });

  it("a type the API refuses (415) shows the API's message on the file's row", async () => {
    on("PUT", /\/documents\//, () => ({ status: 415, body: { detail: "the file's content does not match its declared type. It looks like image/png, not application/yaml.", code: "unsupported_content", detected_type: "image/png" } }));
    const { sheet } = renderUpload();
    fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), { dataTransfer: { files: [new File(["k: v"], "binding.yaml")] } });
    fireEvent.click(within(sheet).getByTestId("knowledge-upload-submit"));
    await waitFor(() => expect(statuses(sheet)).toEqual(["failed"]));
    expect(within(sheet).getByTestId("knowledge-upload-file")).toHaveTextContent("It looks like image/png, not application/yaml.");
  });

  it("the paste form offers reStructuredText, YAML and JSON", () => {
    const { sheet } = renderUpload();
    fireEvent.mouseDown(within(sheet).getByTestId("knowledge-upload-tab-paste"));
    const options = Array.from(within(sheet).getByTestId("knowledge-paste-format").querySelectorAll("option")).map((o) => o.getAttribute("value"));
    expect(options).toEqual(["text/markdown", "text/plain", "text/html", "text/x-rst", "application/yaml", "application/json"]);
  });
});

describe("DocumentSheet", () => {
  const extraction = { extractor: "pdf", extractor_version: 1, current_version: 2, doc_version: 1, run_id: "run-1", sections: 340, chars: 900000, pages: 212, image_only_pages: [3, 7, 9], warnings: 0 };

  function renderSheet(extra: Partial<KnowledgeDocument> = {}) {
    on("GET", /^\/knowledge-bases\/kb1\/documents\/trm\.pdf$/, () => ({ body: doc("trm.pdf", { extraction, ...extra }) }));
    const onLoaded = vi.fn();
    const onReextracted = vi.fn();
    render(<DocumentSheet kbId="kb1" documentKey="trm.pdf" listed={null} progress={null} onOpenChange={() => undefined} onDeleted={() => undefined} onLoaded={onLoaded} onReextracted={onReextracted} />);
    return { sheet: screen.getByTestId("knowledge-document-sheet"), onLoaded, onReextracted };
  }

  it("shows the extractor line, an outdated note, the image-only warning, and no content button for a PDF", async () => {
    const { sheet } = renderSheet();
    expect(await within(sheet).findByTestId("knowledge-document-extractor")).toHaveTextContent("PDF extractor v1 · 212 pages · 340 sections");
    expect(within(sheet).getByTestId("knowledge-document-extractor-outdated")).toHaveTextContent("Extractor v2 is available.");
    expect(within(sheet).getByTestId("knowledge-document-image-only")).toHaveTextContent("3 pages have no text: scanned? (pp. 3, 7, 9)");
    expect(within(sheet).getByTestId("knowledge-document-format")).toHaveTextContent("PDF application/pdf");
    expect(within(sheet).getByTestId("knowledge-document-binary")).toBeInTheDocument();
    expect(within(sheet).queryByTestId("knowledge-document-show-content")).toBeNull();
  });

  it("a failed extraction shows its reason", async () => {
    const { sheet } = renderSheet({ status: "failed", error: "extraction_failed: The PDF is encrypted with a password.", error_code: "extraction_failed", error_reason: "document_encrypted", extraction: null });
    expect(await within(sheet).findByTestId("knowledge-document-sheet-error")).toHaveTextContent("The PDF is encrypted with a password.");
    expect(within(sheet).queryByTestId("knowledge-document-extractor")).toBeNull();
  });

  it("Re-extract asks first, then posts one client run_id (the same on a retry after an error)", async () => {
    let fail = true;
    on("POST", /\/documents\/trm\.pdf\/reextract$/, () => (fail
      ? { status: 503, body: { detail: "the run could not be queued; retry" } }
      : { status: 202, body: { run_id: "run-2", kind: "kb_ingest", knowledge_base_id: "kb1", status: "queued", document: doc("trm.pdf", { latest_version: 2, status: "pending", last_run_id: "run-2" }) } }));
    const { sheet, onReextracted, onLoaded } = renderSheet();
    await within(sheet).findByTestId("knowledge-document-extractor");
    fireEvent.click(within(sheet).getByTestId("knowledge-document-reextract"));
    const dialog = await screen.findByRole("dialog", { name: "Re-extract trm.pdf?" });
    expect(dialog).toHaveTextContent("A new version (v2)");
    expect(sent("POST", /reextract$/)).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Re-extract" }));
    expect(await within(dialog).findByTestId("confirm-dialog-error")).toHaveTextContent("the run could not be queued; retry");
    fail = false;
    fireEvent.click(within(dialog).getByRole("button", { name: "Re-extract" }));
    await waitFor(() => expect(onReextracted).toHaveBeenCalledTimes(1));
    const ids = sent("POST", /reextract$/).map((r) => (r.body as { run_id: string }).run_id);
    expect(ids).toHaveLength(2);
    expect(ids[0]).toMatch(/^[0-9a-f-]{36}$/);
    expect(ids[1]).toBe(ids[0]);
    expect(await within(sheet).findByTestId("knowledge-document-reextract-note")).toHaveTextContent("Re-extracting as v2.");
    expect(onLoaded).toHaveBeenLastCalledWith(expect.objectContaining({ latest_version: 2 }));
    expect(screen.queryByRole("dialog", { name: "Re-extract trm.pdf?" })).toBeNull();
  });

  it("a text document keeps Show content and shows no extraction when the API sends none", async () => {
    on("GET", /^\/knowledge-bases\/kb1\/documents\/a\.md$/, () => ({ body: doc("a.md", { mime_type: "text/markdown", content_encoding: null, extraction: null }) }));
    render(<DocumentSheet kbId="kb1" documentKey="a.md" listed={null} progress={null} onOpenChange={() => undefined} onDeleted={() => undefined} onLoaded={() => undefined} />);
    const sheet = screen.getByTestId("knowledge-document-sheet");
    expect(await within(sheet).findByTestId("knowledge-document-show-content")).toBeInTheDocument();
    expect(within(sheet).queryByTestId("knowledge-document-extractor")).toBeNull();
    expect(within(sheet).queryByTestId("knowledge-document-image-only")).toBeNull();
  });
});

describe("KnowledgeBaseDialog: Advanced extraction", () => {
  beforeEach(() => {
    on("GET", /^\/embedding-models$/, () => ({ body: { items: [{ id: "test:hash", provider: "test", name: "hash", dimensions: 1536, wildcard: false }] } }));
    on("POST", /^\/knowledge-bases$/, (_url, body) => ({ status: 201, body: { id: "kb9", ...(body as object) } }));
  });

  async function fillBasics() {
    render(<KnowledgeBaseDialog onCreated={() => undefined} onClose={() => undefined} />);
    fireEvent.change(screen.getByTestId("knowledge-create-name"), { target: { value: "TI SDK" } });
    const picker = await screen.findByTestId("knowledge-embedding-model");
    await waitFor(() => expect(picker).not.toBeDisabled());
    fireEvent.change(picker, { target: { value: "test:hash" } });
  }

  it("sends no chunking when nothing is set", async () => {
    await fillBasics();
    fireEvent.click(screen.getByTestId("knowledge-create-submit"));
    await waitFor(() => expect(sent("POST", /^\/knowledge-bases$/)).toHaveLength(1));
    expect(sent("POST", /^\/knowledge-bases$/)[0].body).toEqual({ name: "TI SDK", description: null, embedding_model: "test:hash" });
  });

  it("sends chunking.extract with exactly the fields that are set", async () => {
    await fillBasics();
    fireEvent.click(screen.getByTestId("knowledge-create-advanced"));
    fireEvent.change(await screen.findByTestId("knowledge-create-markdown-flavor"), { target: { value: "doxygen" } });
    fireEvent.change(screen.getByTestId("knowledge-create-rst-config"), { target: { value: "CONFIG_part_variant: AM62X" } });
    fireEvent.change(screen.getByTestId("knowledge-create-rst-tags"), { target: { value: "am62x, linux" } });
    fireEvent.click(screen.getByTestId("knowledge-create-submit"));
    await waitFor(() => expect(sent("POST", /^\/knowledge-bases$/)).toHaveLength(1));
    expect(sent("POST", /^\/knowledge-bases$/)[0].body).toEqual({
      name: "TI SDK", description: null, embedding_model: "test:hash",
      chunking: { extract: { markdown_flavor: "doxygen", rst_config: { CONFIG_part_variant: "AM62X" }, rst_tags: ["am62x", "linux"] } },
    });
  });

  it("an invalid name is refused before any request", async () => {
    await fillBasics();
    fireEvent.click(screen.getByTestId("knowledge-create-advanced"));
    fireEvent.change(await screen.findByTestId("knowledge-create-rst-tags"), { target: { value: "bad-tag" } });
    fireEvent.click(screen.getByTestId("knowledge-create-submit"));
    expect(await screen.findByTestId("knowledge-create-error")).toHaveTextContent('"bad-tag" is not a valid tag');
    expect(sent("POST", /^\/knowledge-bases$/)).toHaveLength(0);
  });
});

describe("ReextractDialog (Re-extract outdated)", () => {
  const result = (selected: number, extra: Record<string, unknown> = {}) => ({ knowledge_base_id: "kb1", dry_run: false, chunker_version: 1, chunker_version_changed: false, selected, started: selected, failed: 0, documents: [], ...extra });

  it("dry-runs the outdated selection, then starts it", async () => {
    on("POST", /^\/knowledge-bases\/kb1\/reextract$/, (_url, body) => ({ status: (body as { dry_run?: boolean }).dry_run ? 200 : 202, body: result(3, { dry_run: !!(body as { dry_run?: boolean }).dry_run }) }));
    const onStarted = vi.fn();
    render(<ReextractDialog kbId="kb1" chunkerVersion={2} onStarted={onStarted} onClose={() => undefined} />);
    expect(await screen.findByTestId("knowledge-reextract-count")).toHaveTextContent("3 documents will be re-extracted.");
    expect(screen.queryByTestId("knowledge-reextract-chunker-v2")).toBeNull();
    fireEvent.click(screen.getByTestId("knowledge-reextract-start"));
    await waitFor(() => expect(onStarted).toHaveBeenCalledTimes(1));
    expect(sent("POST", /reextract$/).map((r) => r.body)).toEqual([{ only_outdated: true, dry_run: true }, { only_outdated: true }]);
  });

  it("on chunker v1 it offers chunker v2 (every document); nothing selected disables Start", async () => {
    on("POST", /^\/knowledge-bases\/kb1\/reextract$/, (_url, body) => ({ body: result((body as { chunker_version?: number }).chunker_version ? 5 : 0, { dry_run: true }) }));
    render(<ReextractDialog kbId="kb1" chunkerVersion={1} onStarted={() => undefined} onClose={() => undefined} />);
    expect(await screen.findByTestId("knowledge-reextract-count")).toHaveTextContent("Every document is up to date.");
    expect(screen.getByTestId("knowledge-reextract-start")).toBeDisabled();
    fireEvent.click(screen.getByTestId("knowledge-reextract-chunker-v2"));
    await waitFor(() => expect(screen.getByTestId("knowledge-reextract-count")).toHaveTextContent("5 documents will be re-extracted."));
    expect(sent("POST", /reextract$/).map((r) => r.body)).toEqual([{ only_outdated: true, dry_run: true }, { chunker_version: 2, dry_run: true }]);
  });

  it("request bodies and the started line", () => {
    expect(reextractRequest(false, false)).toEqual({ only_outdated: true });
    expect(reextractRequest(true, true)).toEqual({ chunker_version: 2, dry_run: true });
    expect(reextractStartedText({ started: 1, failed: 0, chunker_version_changed: false })).toBe("Re-extract started for 1 document.");
    expect(reextractStartedText({ started: 4, failed: 1, chunker_version_changed: true })).toBe("Re-extract started for 4 documents. The knowledge base now uses chunker v2. 1 document could not be queued; Retry them from the list.");
  });
});

describe("the retrieve span shows a PDF hit's page", () => {
  it("from #page=N in the hit's URL, or its page fields", () => {
    const span: TraceSpan = {
      id: "s1", parent_id: null, kind: "retrieve", name: "retrieve", start: T0, end: T0, status: "ok", input: null, output: null, truncated: {},
      attributes: {
        query: "uart divisor", hits: [
          { n: 1, title: "TRM", url: "https://ti.example/trm.pdf#page=212", ref: "https://ti.example/trm.pdf#page=212", kb_id: "kb1", chunk_id: "c1", doc_version: 1, score: 0.9 },
          { n: 2, title: "TRM2", url: null, ref: "kb:trm2.pdf", kb_id: "kb1", chunk_id: "c2", doc_version: 1, score: 0.8, page: 40, page_end: 41 },
          { n: 3, title: "Guide", url: "https://ti.example/guide.html#uart", kb_id: "kb1", chunk_id: "c3", score: 0.7 },
        ],
      },
    };
    render(<SpanDetail span={span} />);
    const metas = screen.getAllByTestId("span-retrieve-hit-meta");
    expect(metas.map((el) => el.getAttribute("data-page"))).toEqual(["p. 212", "pp. 40–41", null]);
    expect(metas[0]).toHaveTextContent("p. 212");
  });
});

describe("axe", () => {
  // The page title comes from the app layout, which these component renders do not mount.
  beforeEach(() => {
    document.title = "Knowledge · Studio";
    document.documentElement.lang = "en";
  });

  it("the document Sheet with an extraction and image-only pages, and its Re-extract confirm", async () => {
    on("GET", /^\/knowledge-bases\/kb1\/documents\/trm\.pdf$/, () => ({ body: doc("trm.pdf", { extraction: { extractor: "pdf", extractor_version: 1, current_version: 2, pages: 4, sections: 6, image_only_pages: [2] } }) }));
    render(<DocumentSheet kbId="kb1" documentKey="trm.pdf" listed={null} progress={null} onOpenChange={() => undefined} onDeleted={() => undefined} onLoaded={() => undefined} />);
    const sheet = screen.getByTestId("knowledge-document-sheet");
    await within(sheet).findByTestId("knowledge-document-image-only");
    await expectNoAxeViolations();
    fireEvent.click(within(sheet).getByTestId("knowledge-document-reextract"));
    await screen.findByRole("dialog", { name: "Re-extract trm.pdf?" });
    await expectNoAxeViolations();
  }, AXE_TIMEOUT_MS);

  it("the create dialog with Advanced extraction open, and the Re-extract outdated dialog", async () => {
    on("GET", /^\/embedding-models$/, () => ({ body: { items: [{ id: "test:hash", provider: "test", name: "hash", dimensions: 1536, wildcard: false }] } }));
    on("POST", /^\/knowledge-bases\/kb1\/reextract$/, () => ({ body: { knowledge_base_id: "kb1", dry_run: true, chunker_version: 1, chunker_version_changed: false, selected: 2, started: 0, failed: 0, documents: [] } }));
    const { unmount } = render(<KnowledgeBaseDialog onCreated={() => undefined} onClose={() => undefined} />);
    fireEvent.click(screen.getByTestId("knowledge-create-advanced"));
    await screen.findByTestId("knowledge-create-rst-tags");
    await expectNoAxeViolations();
    unmount();
    render(<ReextractDialog kbId="kb1" chunkerVersion={1} onStarted={() => undefined} onClose={() => undefined} />);
    await screen.findByTestId("knowledge-reextract-count");
    await expectNoAxeViolations();
  }, AXE_TIMEOUT_MS);
});
