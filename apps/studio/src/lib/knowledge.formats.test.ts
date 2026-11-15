// RAG P1.A5: the native formats in Studio's knowledge client (the upload registry and its caps, PDF
// as base64, the re-extract calls, extraction summaries and the "Advanced extraction" config).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  base64DecodedLength,
  buildExtractConfig,
  bytesToBase64,
  capFor,
  checkUploadFile,
  chunkerVersionOf,
  DEFAULT_MAX_BINARY_BYTES,
  DEFAULT_MAX_DOCUMENT_BYTES,
  DEFAULT_MAX_STRUCTURED_BYTES,
  DEFAULT_UPLOAD_CAPS,
  describeDocumentError,
  describeIngestError,
  documentPutBytes,
  documentPutProblem,
  EMPTY_EXTRACT_FORM,
  emptyExtractionText,
  extractionOutdated,
  extractionSummary,
  formatLabel,
  imageOnlyPagesText,
  newClientRunId,
  pageListText,
  putDocument,
  readUploadContent,
  reextractDocument,
  reextractKnowledgeBase,
  UPLOAD_ACCEPT,
  UPLOAD_FORMATS,
  uploadCapsText,
  uploadFormatForFile,
  uploadFormatForMime,
  uploadGuardBytes,
} from "./knowledge";

const MiB = 1024 * 1024;

describe("the upload registry mirrors eap_knowledge.formats", () => {
  it("maps every extension (any case) to its canonical MIME type, binary flag and cap class", () => {
    const table = Object.fromEntries(UPLOAD_FORMATS.flatMap((f) => f.extensions.map((ext) => [ext, [f.mimeType, f.binary, f.capClass]])));
    expect(table).toEqual({
      ".md": ["text/markdown", false, "text"],
      ".markdown": ["text/markdown", false, "text"],
      ".html": ["text/html", false, "text"],
      ".htm": ["text/html", false, "text"],
      ".txt": ["text/plain", false, "text"],
      ".text": ["text/plain", false, "text"],
      ".rst": ["text/x-rst", false, "text"],
      ".rest": ["text/x-rst", false, "text"],
      ".pdf": ["application/pdf", true, "binary"],
      ".yaml": ["application/yaml", false, "structured"],
      ".yml": ["application/yaml", false, "structured"],
      ".json": ["application/json", false, "structured"],
    });
    expect(uploadFormatForFile("Manual.PDF")?.format).toBe("pdf");
    expect(uploadFormatForFile("README")).toBeNull();
    expect(uploadFormatForFile("a.docx")).toBeNull();
  });

  it("aliases and parameters resolve to the format; labels name it", () => {
    expect(uploadFormatForMime("text/yaml")?.mimeType).toBe("application/yaml");
    expect(uploadFormatForMime("application/x-yaml; charset=utf-8")?.format).toBe("yaml");
    expect(uploadFormatForMime("TEXT/PRS.FALLENSTEIN.RST")?.format).toBe("rst");
    expect(uploadFormatForMime("image/png")).toBeNull();
    expect(formatLabel("application/pdf")).toBe("PDF");
    expect(formatLabel("text/x-rst")).toBe("reStructuredText");
    expect(formatLabel("image/png")).toBe("image/png");
  });

  it("the file input accepts every extension plus the PDF type", () => {
    for (const ext of [".md", ".rst", ".pdf", ".yaml", ".yml", ".json"]) expect(UPLOAD_ACCEPT.split(",")).toContain(ext);
    expect(UPLOAD_ACCEPT.split(",")).toContain("application/pdf");
  });
});

describe("per-type caps (EAP_KB_MAX_DOCUMENT_BYTES, _STRUCTURED_BYTES, _BINARY_BYTES)", () => {
  it("defaults: text 5 MiB, YAML/JSON 2 MiB, PDF 25 MiB; the guard is max(text, ceil(binary × 4/3) + 64 KiB)", () => {
    expect(DEFAULT_UPLOAD_CAPS).toEqual({ text: 5 * MiB, structured: 2 * MiB, binary: 25 * MiB });
    expect(capFor({ capClass: "text" })).toBe(DEFAULT_MAX_DOCUMENT_BYTES);
    expect(capFor({ capClass: "structured" })).toBe(DEFAULT_MAX_STRUCTURED_BYTES);
    expect(capFor({ capClass: "binary" })).toBe(DEFAULT_MAX_BINARY_BYTES);
    // structured is also bounded by the text cap
    expect(capFor({ capClass: "structured" }, { text: 1 * MiB, structured: 2 * MiB, binary: 25 * MiB })).toBe(1 * MiB);
    expect(uploadGuardBytes()).toBe(Math.ceil((25 * MiB * 4) / 3) + 64 * 1024);
    expect(uploadGuardBytes({ text: 100 * MiB, structured: 2 * MiB, binary: 1 })).toBe(100 * MiB);
    expect(uploadCapsText()).toBe("Text up to 5 MiB, YAML and JSON up to 2 MiB, PDF up to 25 MiB.");
  });

  it("checkUploadFile applies each format's cap and says how it is sent", () => {
    expect(checkUploadFile({ name: "m.pdf", size: 25 * MiB })).toEqual({ ok: true, mimeType: "application/pdf", key: "m.pdf", title: "m", binary: true, maxBytes: 25 * MiB });
    expect(checkUploadFile({ name: "m.pdf", size: 25 * MiB + 1 })).toEqual({ ok: false, code: "too_large", reason: "The file is larger than 25 MiB." });
    expect(checkUploadFile({ name: "b.yaml", size: 2 * MiB }).ok).toBe(true);
    expect(checkUploadFile({ name: "b.yml", size: 2 * MiB + 1 })).toEqual({ ok: false, code: "too_large", reason: "The file is larger than 2 MiB." });
    expect(checkUploadFile({ name: "c.json", size: 3 * MiB })).toEqual({ ok: false, code: "too_large", reason: "The file is larger than 2 MiB." });
    expect(checkUploadFile({ name: "d.rst", size: 5 * MiB })).toMatchObject({ ok: true, mimeType: "text/x-rst", binary: false });
    expect(checkUploadFile({ name: "d.rst", size: 5 * MiB + 1 })).toEqual({ ok: false, code: "too_large", reason: "The file is larger than 5 MiB." });
    expect(checkUploadFile({ name: "x.docx", size: 10 })).toEqual({ ok: false, code: "unsupported_type", reason: "Unsupported file type: upload .md, .txt, .html, .rst, .pdf, .yaml, .yml or .json files." });
  });

  it("documentPutProblem: text keeps the JSON body cap; YAML/JSON content its 2 MiB; base64 the guard and the decoded binary cap", () => {
    const key = "k";
    expect(documentPutProblem(key, { content: "x".repeat(2 * MiB), mime_type: "application/yaml" })).toBeNull();
    expect(documentPutProblem(key, { content: "x".repeat(2 * MiB + 1), mime_type: "application/json" })).toBe("The file is larger than 2 MiB.");
    const overhead = documentPutBytes(key, { content: "", mime_type: "text/x-rst" });
    expect(documentPutProblem(key, { content: "x".repeat(DEFAULT_MAX_DOCUMENT_BYTES - overhead), mime_type: "text/x-rst" })).toBeNull();
    expect(documentPutProblem(key, { content: "x".repeat(DEFAULT_MAX_DOCUMENT_BYTES - overhead + 1), mime_type: "text/x-rst" })).toBe("The upload is larger than 5 MiB once encoded (JSON).");
    // A PDF over 5 MiB is fine: its body is capped by the guard, its bytes by the binary cap.
    const pdf = (bytes: number) => "A".repeat(Math.ceil(bytes / 3) * 4);
    expect(documentPutProblem(key, { content_base64: pdf(24 * MiB), mime_type: "application/pdf" })).toBeNull();
    expect(documentPutProblem(key, { content_base64: pdf(25 * MiB + 3), mime_type: "application/pdf" })).toBe("The file is larger than 25 MiB.");
  });
});

describe("binary uploads are base64", () => {
  it("bytesToBase64 matches standard base64 (padding included) across slice boundaries", () => {
    expect(bytesToBase64(new TextEncoder().encode("%PDF-1.7"))).toBe("JVBERi0xLjc=");
    const big = new Uint8Array(0x8000 * 2 + 5).map((_, i) => i % 251);
    const b64 = bytesToBase64(big);
    expect(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))).toEqual(big);
    expect(base64DecodedLength(b64)).toBe(big.length);
    expect(base64DecodedLength("JVBERi0xLjc=")).toBe(8);
    expect(base64DecodedLength("QQ==")).toBe(1);
    expect(base64DecodedLength("")).toBe(0);
  });

  it("readUploadContent sends a PDF as content_base64 and text as UTF-8 content", async () => {
    const bytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0xff, 0x00, 0x80]);
    expect(await readUploadContent(new Blob([bytes]), { binary: true })).toEqual({ content_base64: bytesToBase64(bytes) });
    expect(await readUploadContent(new Blob(["title: x\n"]), { binary: false })).toEqual({ content: "title: x\n" });
  });
});

describe("re-extract requests", () => {
  type Call = { url: URL; init: RequestInit };
  let calls: Call[];
  let reply: () => Response;
  beforeEach(() => {
    calls = [];
    reply = () => new Response(JSON.stringify({}), { status: 202, headers: { "content-type": "application/json" } });
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit = {}) => {
      calls.push({ url: new URL(input), init });
      return reply();
    }));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("POST …/documents/{key}/reextract with the client run_id (or an empty body)", async () => {
    await reextractDocument("kb 1", "docs/a.pdf", "11111111-1111-4111-8111-111111111111");
    await reextractDocument("kb 1", "a.pdf");
    expect(calls.map((c) => [c.init.method, c.url.pathname, c.init.body])).toEqual([
      ["POST", "/api/v1/knowledge-bases/kb%201/documents/docs%2Fa.pdf/reextract", JSON.stringify({ run_id: "11111111-1111-4111-8111-111111111111" })],
      ["POST", "/api/v1/knowledge-bases/kb%201/documents/a.pdf/reextract", "{}"],
    ]);
  });

  it("POST /knowledge-bases/{kb}/reextract carries the body as given", async () => {
    await reextractKnowledgeBase("kb1", { only_outdated: true, dry_run: true });
    await reextractKnowledgeBase("kb1", { chunker_version: 2 });
    expect(calls.map((c) => [c.url.pathname, JSON.parse(String(c.init.body))])).toEqual([
      ["/api/v1/knowledge-bases/kb1/reextract", { only_outdated: true, dry_run: true }],
      ["/api/v1/knowledge-bases/kb1/reextract", { chunker_version: 2 }],
    ]);
  });

  it("putDocument sends content_base64 and the PDF type, never `content`", async () => {
    await putDocument("kb1", "m.pdf", { title: "m", content_base64: "JVBERi0=", mime_type: "application/pdf" });
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ document_key: "m.pdf", title: "m", content_base64: "JVBERi0=", mime_type: "application/pdf" });
  });

  it("a 415 surfaces the API's detail as the error message", async () => {
    reply = () => new Response(JSON.stringify({ detail: "the file's content does not match application/pdf.", code: "unsupported_content", detected_type: null }), { status: 415, headers: { "content-type": "application/json" } });
    await expect(putDocument("kb1", "m.pdf", { content_base64: "QQ==", mime_type: "application/pdf" })).rejects.toMatchObject({ status: 415, message: "the file's content does not match application/pdf." });
  });

  it("newClientRunId is a UUID v4, also without crypto.randomUUID", () => {
    const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
    expect(newClientRunId()).toMatch(uuid);
    const original = crypto.randomUUID;
    try {
      Object.defineProperty(crypto, "randomUUID", { value: undefined, configurable: true });
      const id = newClientRunId();
      expect(id).toMatch(uuid);
      expect(newClientRunId()).not.toBe(id);
    } finally {
      Object.defineProperty(crypto, "randomUUID", { value: original, configurable: true });
    }
  });
});

describe("extraction and errors", () => {
  it("extractionSummary, outdated and image-only pages", () => {
    const pdf = { extractor: "pdf", extractor_version: 1, current_version: 1, pages: 212, sections: 340, warnings: 0, image_only_pages: [3, 7, 9] };
    expect(extractionSummary(pdf)).toBe("PDF extractor v1 · 212 pages · 340 sections");
    expect(extractionSummary({ extractor: "rst", extractor_version: 1, sections: 1, conditions: 2, warnings: 1 })).toBe("reStructuredText extractor v1 · 1 section · 2 conditions · 1 warning");
    expect(extractionSummary({ extractor: "future", extractor_version: 3 })).toBe("future extractor v3");
    expect(extractionOutdated(pdf)).toBe(false);
    expect(extractionOutdated({ extractor_version: 1, current_version: 2 })).toBe(true);
    expect(extractionOutdated({ extractor_version: 1, current_version: null })).toBe(false);
    expect(extractionOutdated(null)).toBe(false);
    expect(imageOnlyPagesText([3, 7, 9])).toBe("3 pages have no text: scanned?");
    expect(imageOnlyPagesText([4])).toBe("1 page has no text: scanned?");
    expect(imageOnlyPagesText([])).toBeNull();
    expect(imageOnlyPagesText(null)).toBeNull();
    expect(pageListText([3, 7, 9])).toBe("pp. 3, 7, 9");
    expect(pageListText([4])).toBe("p. 4");
    expect(pageListText([1, 2, 3, 4], 2)).toBe("pp. 1, 2, …");
  });

  it("an empty document (ready, 0 chunks) explains itself", () => {
    expect(emptyExtractionText({ empty_reason: "no_extractable_text", extractor: "rst" })).toBe(
      "No extractable text: nothing is indexed. Expected for a page with only toctree or include directives.",
    );
    expect(emptyExtractionText({ empty_reason: "no_extractable_text" })).toBe("No extractable text: nothing is indexed.");
    expect(emptyExtractionText({ empty_reason: null })).toBeNull();
    expect(emptyExtractionText({})).toBeNull();
    expect(emptyExtractionText(null)).toBeNull();
  });

  it("an extraction failure shows its fixed reason", () => {
    expect(describeIngestError("extraction_failed: The PDF is encrypted with a password.")).toBe("The PDF is encrypted with a password.");
    expect(describeIngestError("extraction_failed: unsupported MIME type")).toBe("No text could be extracted from the document. (unsupported MIME type)");
    expect(describeDocumentError({ error: "extraction_failed: anything", error_reason: "extract_timeout" })).toBe("Extraction took too long and was stopped.");
    expect(describeDocumentError({ error: "embedding_failed: HTTP 500", error_reason: null })).toBe("The embedding provider failed. (HTTP 500)");
  });

  it("chunkerVersionOf reads chunking.chunker_version (1 when absent)", () => {
    expect(chunkerVersionOf({ chunking: { max_tokens: 1, overlap_tokens: 0, min_tokens: 1, chunker_version: 2 } })).toBe(2);
    expect(chunkerVersionOf({ chunking: { max_tokens: 1, overlap_tokens: 0, min_tokens: 1 } })).toBe(1);
    expect(chunkerVersionOf(null)).toBe(1);
  });
});

describe("Advanced extraction: only the fields that are set", () => {
  it("an empty form is null (no extract in the request)", () => {
    expect(buildExtractConfig(EMPTY_EXTRACT_FORM)).toEqual({ ok: true, value: null });
    expect(buildExtractConfig({ ...EMPTY_EXTRACT_FORM, rstConfig: "  \n ", rstTags: " , " })).toEqual({ ok: true, value: null });
  });

  it("each field on its own, typed scalars, unique tags", () => {
    expect(buildExtractConfig({ ...EMPTY_EXTRACT_FORM, markdownFlavor: "doxygen" })).toEqual({ ok: true, value: { markdown_flavor: "doxygen" } });
    expect(buildExtractConfig({ ...EMPTY_EXTRACT_FORM, pdfHeadings: "font" })).toEqual({ ok: true, value: { pdf_headings: "font" } });
    expect(buildExtractConfig({ ...EMPTY_EXTRACT_FORM, rstConfig: "CONFIG_part_variant: AM62X\nCONFIG_count = 3\nCONFIG_on: true\nQUOTED: \"7\"" })).toEqual({
      ok: true,
      value: { rst_config: { CONFIG_part_variant: "AM62X", CONFIG_count: 3, CONFIG_on: true, QUOTED: "7" } },
    });
    expect(buildExtractConfig({ ...EMPTY_EXTRACT_FORM, rstTags: "am62x, linux\nam62x  rtos" })).toEqual({ ok: true, value: { rst_tags: ["am62x", "linux", "rtos"] } });
  });

  it("names follow the API's pattern", () => {
    expect(buildExtractConfig({ ...EMPTY_EXTRACT_FORM, rstConfig: "1abc: x" })).toMatchObject({ ok: false, reason: expect.stringMatching(/"1abc" is not a valid name/) });
    expect(buildExtractConfig({ ...EMPTY_EXTRACT_FORM, rstConfig: "no separator" })).toMatchObject({ ok: false, reason: expect.stringMatching(/is not NAME: value/) });
    expect(buildExtractConfig({ ...EMPTY_EXTRACT_FORM, rstTags: "ok bad-tag" })).toMatchObject({ ok: false, reason: expect.stringMatching(/"bad-tag" is not a valid tag/) });
    const many = Array.from({ length: 65 }, (_, i) => `t${i}`).join(" ");
    expect(buildExtractConfig({ ...EMPTY_EXTRACT_FORM, rstTags: many })).toMatchObject({ ok: false, reason: "rst tags: at most 64 tags." });
  });
});
