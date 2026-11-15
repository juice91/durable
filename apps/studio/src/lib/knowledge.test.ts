import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ingestStuck,
  ingestStuckReason,
  STUCK_INGEST_TEXT,
  STUCK_PENDING_AFTER_MS,
  checkUploadFile,
  decodeUtf8,
  NotUtf8Error,
  readFileAsText,
  deleteKnowledgeBase,
  describeIngestError,
  documentKeyFrom,
  documentKeyProblem,
  ingestProgress,
  isSafeHttpUrl,
  KnowledgeBaseInUseError,
  knowledgeBaseIdsOf,
  listDocuments,
  listKnowledgeBaseRuns,
  listKnowledgeBases,
  mimeTypeForFile,
  parseFilter,
  parseMetadata,
  progressLabel,
  putDocument,
  searchKnowledgeBase,
} from "./knowledge";
import type { AgentSpec } from "./api";
import type { RunEvent } from "./runs";

type Call = { url: URL; init: RequestInit };
let calls: Call[];
let answer: (url: URL, init: RequestInit) => Response;

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeEach(() => {
  calls = [];
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input);
      calls.push({ url, init });
      return answer(url, init);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.useRealTimers();
});

describe("upload helpers", () => {
  it("maps every registry extension (any case) to the API's MIME types and refuses the rest", () => {
    expect(mimeTypeForFile("a.md")).toBe("text/markdown");
    expect(mimeTypeForFile("A.MARKDOWN")).toBe("text/markdown");
    expect(mimeTypeForFile("notes.txt")).toBe("text/plain");
    expect(mimeTypeForFile("page.HTM")).toBe("text/html");
    expect(mimeTypeForFile("manual.PDF")).toBe("application/pdf");
    expect(mimeTypeForFile("intro.rst")).toBe("text/x-rst");
    expect(mimeTypeForFile("binding.yml")).toBe("application/yaml");
    expect(mimeTypeForFile("data.json")).toBe("application/json");
    for (const name of ["doc.docx", "README", "archive.md.zip", "image.png"]) expect(mimeTypeForFile(name), name).toBeNull();
  });

  it("checkUploadFile: key from the file name, title without the extension; refuses type, empty and oversized files", () => {
    expect(checkUploadFile({ name: "Getting started.md", size: 10 })).toEqual({ ok: true, mimeType: "text/markdown", key: "Getting started.md", title: "Getting started", binary: false, maxBytes: 5 * 1024 * 1024 });
    expect(checkUploadFile({ name: "x.docx", size: 10 })).toMatchObject({ ok: false, reason: expect.stringMatching(/Unsupported file type/) });
    expect(checkUploadFile({ name: "x.md", size: 0 })).toMatchObject({ ok: false, reason: "The file is empty." });
    expect(checkUploadFile({ name: "x.md", size: 6 * 1024 * 1024 })).toMatchObject({ ok: false, reason: expect.stringMatching(/larger than 5 MiB/) });
    expect(checkUploadFile({ name: "x.md", size: 200 }, 100)).toMatchObject({ ok: false });
  });

  it("document keys follow the API's rule: no / or \\, no control characters, trimmed, not only dots", () => {
    expect(documentKeyFrom("a/b\\c.md")).toBe("a-b-c.md");
    expect(documentKeyFrom("  spaced.md  ")).toBe("spaced.md");
    expect(documentKeyFrom("tab\there")).toBe("tab-here");
    expect(documentKeyFrom("..")).toBeNull();
    expect(documentKeyFrom("   ")).toBeNull();
    expect(documentKeyFrom("x".repeat(300))).toHaveLength(256);
    expect(documentKeyProblem("ok.md")).toBeNull();
    expect(documentKeyProblem("a/b")).toMatch(/must not contain/);
    expect(documentKeyProblem(" lead")).toMatch(/surrounding/);
    expect(documentKeyProblem("...")).toMatch(/only dots/);
    expect(documentKeyProblem("")).toMatch(/1 to 256/);
  });

  it("parseMetadata: key: value lines, typed scalars, quoted strings, at most 16 keys, valid key names", () => {
    expect(parseMetadata("")).toEqual({ ok: true, value: null });
    expect(parseMetadata("lang: en\ntier=2\nbeta: true\nid: \"007\"\nratio: 0.5")).toEqual({ ok: true, value: { lang: "en", tier: 2, beta: true, id: "007", ratio: 0.5 } });
    expect(parseMetadata("no separator")).toMatchObject({ ok: false });
    expect(parseMetadata("bad key!: x")).toMatchObject({ ok: false });
    expect(parseMetadata(Array.from({ length: 17 }, (_, i) => `k${i}: v`).join("\n"))).toMatchObject({ ok: false, reason: "At most 16 metadata keys." });
  });

  it("parseFilter: a JSON object of scalars or lists of scalars; empty is no filter", () => {
    expect(parseFilter("  ")).toEqual({ ok: true, value: null });
    expect(parseFilter('{"lang": "en", "tier": [1, 2]}')).toEqual({ ok: true, value: { lang: "en", tier: [1, 2] } });
    expect(parseFilter("{}")).toEqual({ ok: true, value: null });
    expect(parseFilter("[1]")).toMatchObject({ ok: false });
    expect(parseFilter("{nope")).toMatchObject({ ok: false, reason: "The filter is not valid JSON." });
    expect(parseFilter('{"a": {"b": 1}}')).toMatchObject({ ok: false });
  });
});

describe("progress, errors and references", () => {
  const ev = (type: string, data: Record<string, unknown>): Pick<RunEvent, "type" | "data"> => ({ type: type as RunEvent["type"], data });

  it("ingestProgress takes the latest ingest_progress event and ignores others", () => {
    expect(ingestProgress([])).toBeNull();
    const progress = ingestProgress([ev("run_started", {}), ev("ingest_progress", { embedded: 0, total: 12 }), ev("embedding", { inputs: 4 }), ev("ingest_progress", { embedded: 8, total: 12 })]);
    expect(progress).toEqual({ embedded: 8, total: 12 });
    expect(progressLabel(progress!)).toBe("8 of 12 sections embedded");
    expect(progressLabel({ embedded: 1, total: 1 })).toBe("1 of 1 section embedded");
  });

  it("describeIngestError reads '<code>: detail' and keeps unknown text", () => {
    expect(describeIngestError("ingest_budget_exceeded")).toMatch(/more tokens than allowed/);
    expect(describeIngestError("embedding_failed: HTTP 500")).toBe("The embedding provider failed. (HTTP 500)");
    expect(describeIngestError("something odd")).toBe("something odd");
    expect(describeIngestError(null)).toBe("The ingest failed.");
  });

  it("knowledgeBaseIdsOf reads AgentSpec.knowledge_bases and tolerates specs without it", () => {
    const spec = { knowledge_bases: [{ id: "kb1", top_k: 4 }, { id: "kb2" }, { nope: true }] } as unknown as AgentSpec;
    expect(knowledgeBaseIdsOf(spec)).toEqual(["kb1", "kb2"]);
    expect(knowledgeBaseIdsOf({} as AgentSpec)).toEqual([]);
    expect(knowledgeBaseIdsOf(null)).toEqual([]);
  });

  it("isSafeHttpUrl accepts only absolute http(s) URLs", () => {
    expect(isSafeHttpUrl("https://docs.example.com/a#b")).toBe(true);
    expect(isSafeHttpUrl("http://x.test")).toBe(true);
    for (const bad of ["javascript:alert(1)", "data:text/html,x", "//evil.test", "/relative", "", null, undefined]) expect(isSafeHttpUrl(bad), String(bad)).toBe(false);
  });
});

describe("requests", () => {
  it("listKnowledgeBases unwraps items; listDocuments pages with limit and after (encoded)", async () => {
    answer = (url) => (url.pathname.endsWith("/documents") ? json({ items: [], next_after: null }) : json({ items: [{ id: "kb1" }] }));
    expect(await listKnowledgeBases()).toEqual([{ id: "kb1" }]);
    expect(calls[0].url.pathname).toBe("/api/v1/knowledge-bases");
    await listDocuments("kb/1", { limit: 2, after: "abc=" });
    expect(calls[1].url.pathname).toBe("/api/v1/knowledge-bases/kb%2F1/documents");
    expect(calls[1].url.searchParams.get("limit")).toBe("2");
    expect(calls[1].url.searchParams.get("after")).toBe("abc=");
  });

  it("putDocument PUTs JSON with the key in the body; listKnowledgeBaseRuns filters by knowledge base and kind", async () => {
    answer = () => json({ unchanged: false, created: true, document: {}, run_id: "r1" }, 202);
    const result = await putDocument("kb1", "a b.md", { content: "# Hi", mime_type: "text/markdown", title: "A" });
    expect(result.run_id).toBe("r1");
    expect(calls[0].url.pathname).toBe("/api/v1/knowledge-bases/kb1/documents/a%20b.md");
    expect(calls[0].init.method).toBe("PUT");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ document_key: "a b.md", content: "# Hi", mime_type: "text/markdown", title: "A" });
    answer = () => json({ items: [], next_before: null });
    await listKnowledgeBaseRuns("kb1", { before: "c1" });
    expect(Object.fromEntries(calls[1].url.searchParams)).toEqual({ knowledge_base_id: "kb1", limit: "50", kind: "kb_ingest", before: "c1" });
  });

  it("deleteKnowledgeBase: a 409 with agents throws KnowledgeBaseInUseError; force=true is sent when asked", async () => {
    const agents = [{ agent_id: "a1", agent_name: "Support", version: 3, archived: false }];
    answer = (url) => (url.searchParams.get("force") === "true" ? json(null, 204) : json({ detail: "knowledge base is attached to 1 agent(s)", agents }, 409));
    const error = await deleteKnowledgeBase("kb1").catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(KnowledgeBaseInUseError);
    expect((error as KnowledgeBaseInUseError).agents).toEqual(agents);
    expect((error as KnowledgeBaseInUseError).status).toBe(409);
    await expect(deleteKnowledgeBase("kb1", { force: true })).resolves.toBeUndefined();
    expect(calls[1].url.search).toBe("?force=true");
  });

  it("searchKnowledgeBase returns a 200 result at once, and polls the run after a 202 until it completes", async () => {
    const result = { knowledge_base_id: "kb1", embedding_model: "test:hash", hits: [{ n: 1, title: "Install", score: 0.9 }] };
    answer = () => json({ run_id: "s1", status: "completed", trace_id: "t", result });
    expect(await searchKnowledgeBase("kb1", { query: "q", top_k: 3 })).toEqual({ status: "completed", runId: "s1", traceId: "t", result });
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ query: "q", top_k: 3 });

    let polls = 0;
    answer = (url) => {
      if (url.pathname.endsWith("/search")) return json({ run_id: "s2", status: "running" }, 202);
      polls += 1;
      return json(polls < 2 ? { run_id: "s2", status: "running", output: null, trace_id: null } : { run_id: "s2", status: "completed", output: JSON.stringify(result), trace_id: "t2" });
    };
    const outcome = await searchKnowledgeBase("kb1", { query: "q" }, { pollIntervalMs: 1 });
    expect(outcome).toEqual({ status: "completed", runId: "s2", traceId: "t2", result });
    expect(calls.at(-1)?.url.pathname).toBe("/api/v1/runs/s2");

    answer = () => json({ run_id: "s3", status: "failed", error: "embedding_failed" });
    expect(await searchKnowledgeBase("kb1", { query: "q" })).toMatchObject({ status: "failed", error: "embedding_failed" });
  });
});

describe("strict UTF-8 and aborts", () => {
  it("decodeUtf8 refuses invalid UTF-8 instead of producing U+FFFD; readFileAsText rejects such a file", async () => {
    expect(decodeUtf8(new TextEncoder().encode("héllo ✓"))).toBe("héllo ✓");
    expect(() => decodeUtf8(new Uint8Array([0x68, 0xff, 0xfe, 0x69]))).toThrow(NotUtf8Error);
    expect(() => decodeUtf8(new Uint8Array([0xe2, 0x82]))).toThrow(/not valid UTF-8/);
    await expect(readFileAsText(new Blob([new Uint8Array([0xc3, 0x28])]))).rejects.toBeInstanceOf(NotUtf8Error);
    await expect(readFileAsText(new Blob([new TextEncoder().encode("ok é")]))).resolves.toBe("ok é");
  });

  it("an abort stops the search's polling at once, with no further run reads", async () => {
    answer = (url) => (url.pathname.endsWith("/search") ? json({ run_id: "s9", status: "running" }, 202) : json({ run_id: "s9", status: "running", output: null }));
    const controller = new AbortController();
    const pending = searchKnowledgeBase("kb1", { query: "q" }, { pollIntervalMs: 60_000, signal: controller.signal });
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(calls.filter((c) => c.url.pathname.startsWith("/api/v1/runs/"))).toHaveLength(0);
  });
});


describe("ingestStuck", () => {
  const now = Date.parse("2026-11-14T10:05:00Z");
  const fresh = "2026-11-14T10:04:30Z";
  const old = "2026-11-14T10:00:00Z";
  const pending = (updated_at = fresh) => ({ status: "pending" as const, updated_at });

  it("a pending document whose run ended (an enqueue failure is 'failed') is stuck at once", () => {
    expect(ingestStuck(pending(), { status: "failed", created_at: fresh }, now)).toBe(true);
    expect(ingestStuck(pending(), { status: "completed", created_at: fresh }, now)).toBe(true);
  });

  it("a queued run is stuck only past STUCK_PENDING_AFTER_MS; a running one never", () => {
    expect(STUCK_PENDING_AFTER_MS).toBe(60_000);
    expect(ingestStuck(pending(), { status: "queued", created_at: fresh }, now)).toBe(false);
    expect(ingestStuck(pending(), { status: "queued", created_at: old }, now)).toBe(true);
    expect(ingestStuck(pending(old), { status: "running", created_at: old }, now)).toBe(false);
  });

  it("says why: an ended or missing run did not start, an old queued run is still queued", () => {
    expect(ingestStuckReason(pending(), { status: "failed", created_at: fresh }, now)).toBe("not_started");
    expect(ingestStuckReason(pending(old), null, now)).toBe("not_started");
    expect(ingestStuckReason(pending(), { status: "queued", created_at: old }, now)).toBe("queued");
    expect(ingestStuckReason(pending(), { status: "running", created_at: old }, now)).toBeNull();
    expect(STUCK_INGEST_TEXT.queued).toBe("Still queued after a minute. Retry re-queues it.");
    expect(STUCK_INGEST_TEXT.not_started).toMatch(/^The ingest did not start\./);
  });

  it("no run: stuck once the document is old enough; unknown run or another status: never", () => {
    expect(ingestStuck(pending(), null, now)).toBe(false);
    expect(ingestStuck(pending(old), null, now)).toBe(true);
    expect(ingestStuck(pending(old), undefined, now)).toBe(false);
    expect(ingestStuck({ status: "ingesting", updated_at: old }, { status: "failed", created_at: old }, now)).toBe(false);
    expect(ingestStuck({ status: "failed", updated_at: old }, null, now)).toBe(false);
  });
});

describe("referenceLabel (delete 409 references)", () => {
  it("a published version, archived, a draft-only reference, and an older API without `draft`", async () => {
    const { referenceLabel } = await import("./knowledge");
    expect(referenceLabel({ version: 3, archived: false, draft: false })).toBe("v3");
    expect(referenceLabel({ version: 1, archived: true })).toBe("v1 (archived)");
    expect(referenceLabel({ version: null, archived: false, draft: true })).toBe("(draft)");
    expect(referenceLabel({ version: null, archived: true, draft: true })).toBe("(draft, archived)");
  });
});

describe("displayedDocumentStatus", () => {
  it("pending shows ingesting once its run started; other statuses and other runs stay as stored", async () => {
    const { displayedDocumentStatus } = await import("./knowledge");
    const started = new Set(["r1"]);
    expect(displayedDocumentStatus({ status: "pending", last_run_id: "r1" }, started)).toBe("ingesting");
    expect(displayedDocumentStatus({ status: "pending", last_run_id: "r2" }, started)).toBe("pending");
    expect(displayedDocumentStatus({ status: "pending", last_run_id: null }, started)).toBe("pending");
    expect(displayedDocumentStatus({ status: "ready", last_run_id: "r1" }, started)).toBe("ready");
    expect(displayedDocumentStatus({ status: "failed", last_run_id: "r1" }, started)).toBe("failed");
    expect(displayedDocumentStatus({ status: "pending", last_run_id: "r1" }, undefined)).toBe("pending");
  });
});
