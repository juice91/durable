import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CHECK_CODE_TEXT,
  SYNC_WORKFLOW_NAME,
  checkText,
  connectionPayload,
  createConnection,
  listAdapters,
  providerInfo,
  providerLabel,
  resetAdapterCache,
  type AdapterInfo,
  deleteConnection,
  findSyncWorkflow,
  parseLines,
  parseMetadataMap,
  parsePartNumbers,
  publishSyncWorkflow,
  REPOSITORY_PATTERN,
  repositoryConfig,
  reportSummary,
  sourcesUsingConnection,
  startSourceSync,
  syncInput,
} from "./kbSources";

const fetchMock = vi.fn();
beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer dev:t1:u1");
});
afterEach(() => {
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});
const GITHUB: AdapterInfo = { kind: "repository_docs", provider: "github", distribution: "eap-kb-sources", display_name: "GitHub", connection_kind: "api_token", base_url: "https://api.github.com", token_url: null, config_schema: null };
const PLUGIN: AdapterInfo = { kind: "product_catalog", provider: "acme", distribution: "eap-kb-sources-acme", display_name: "Acme Parts", connection_kind: "oauth2_client_credentials", base_url: null, token_url: null, config_schema: null };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const urls = () => fetchMock.mock.calls.map(([url, init]) => `${(init?.method as string) ?? "GET"} ${String(url).replace(/^.*?\/api\/v1/, "")}`);

describe("check codes", () => {
  it("has a stable text for each connection check code", () => {
    for (const code of ["connection_not_found", "connection_disabled", "secret_missing", "connection_auth_failed", "connection_unreachable", "connection_response_invalid"]) {
      expect(CHECK_CODE_TEXT[code]).toBeTruthy();
    }
    expect(checkText({ ok: true, checked_at: "x", error_code: null })).toMatch(/ok/i);
    expect(checkText({ ok: false, checked_at: "x", error_code: "secret_missing" })).toBe(CHECK_CODE_TEXT.secret_missing);
    expect(checkText({ ok: false, checked_at: "x", error_code: "weird_code" })).toContain("weird_code");
  });
});

describe("connectionPayload", () => {
  it("sends refs, never values, and the token URL in config", () => {
    const body = connectionPayload({ name: "ti", provider: "acme", baseUrl: "https://api.acme.test", tokenUrl: "https://auth.acme.test/token", clientIdSecret: "conn-id", clientSecretSecret: "conn-secret", tokenSecret: "" });
    expect(body).toEqual({
      name: "ti",
      kind: "oauth2_client_credentials",
      base_url: "https://api.acme.test",
      config: { provider: "acme", token_url: "https://auth.acme.test/token" },
      credential_refs: { client_id: "secret:conn-id", client_secret: "secret:conn-secret" },
      enabled: true,
    });
  });
  it("leaves an empty token URL out", () => {
    const body = connectionPayload({ name: "ti", provider: "acme", baseUrl: "https://x.test", tokenUrl: " ", clientIdSecret: "a", clientSecretSecret: "b", tokenSecret: "" });
    expect(body.config).toEqual({ provider: "acme" });
  });
});

describe("connectionPayload for an api_token provider", () => {
  const form = { name: "repo", provider: "github", baseUrl: "https://api.github.com", tokenUrl: "", clientIdSecret: "", clientSecretSecret: "", tokenSecret: "" };
  it("sends the optional token as one reference", () => {
    expect(connectionPayload({ ...form, tokenSecret: "repo-token" }, true, [GITHUB])).toEqual({
      name: "repo", kind: "api_token", base_url: "https://api.github.com", config: { provider: "github" },
      credential_refs: { token: "secret:repo-token" }, enabled: true,
    });
  });
  it("sends no reference when there is no token, and no OAuth fields", () => {
    const body = connectionPayload({ ...form, clientIdSecret: "leftover", tokenUrl: "https://x.test" }, true, [GITHUB]);
    expect(body.credential_refs).toEqual({});
    expect(body.config).toEqual({ provider: "github" });
    expect(body.kind).toBe("api_token");
  });
});

describe("parseMetadataMap", () => {
  it("reads the importer's glob: {metadata} mapping in file order", () => {
    const rules = parseMetadataMap('# mcu\napi_guide/device/am263x/**: {sdk_family: mcu-plus-sdk, device: AM263x}\n"**":  {sdk_family: mcu-plus-sdk, rank: 2, public: true}\n');
    expect(rules).toEqual([
      { match: "api_guide/device/am263x/**", metadata: { sdk_family: "mcu-plus-sdk", device: "AM263x" } },
      { match: "**", metadata: { sdk_family: "mcu-plus-sdk", rank: 2, public: true } },
    ]);
  });
  it("reads the rules form and treats empty text as no map", () => {
    expect(parseMetadataMap("rules:\n  - match: '*.md'\n    metadata: {kind: doc}\n")).toEqual([{ match: "*.md", metadata: { kind: "doc" } }]);
    expect(parseMetadataMap("  \n")).toEqual([]);
    expect(parseMetadataMap("# only a comment")).toEqual([]);
  });
  it.each([
    ["a: [", /not valid YAML/],
    ["- a\n- b", /mapping/],
    ["a: 1", /mapping of key: value/],
    ["a: {path: x}", /reserved/],
    ["a: {eap: x}", /reserved/],
    ["a: {k: [1]}", /string, number or boolean/],
    ["a: {k: {n: 1}}", /string, number or boolean/],
    ["a: {k: 1}\na: {k: 2}", /not valid YAML/],
    [`a: {${Array.from({ length: 12 }, (_, i) => `k${i}: 1`).join(", ")}}`, /at most 11/],
    ["rules:\n  - metadata: {a: 1}", /match/],
  ])("refuses %j", (text, message) => {
    expect(() => parseMetadataMap(text)).toThrow(message);
  });
});

describe("repository source config", () => {
  it("builds the config from the form", () => {
    expect(
      repositoryConfig({ repository: " o/r ", ref: " v1 ", include: "docs/**\n\n docs/**\n*.pdf", exclude: "docs/old/**", map: "'**': {family: x}", deleteMissing: true }),
    ).toEqual({
      repository: "o/r", ref: "v1", include: ["docs/**", "*.pdf"], exclude: ["docs/old/**"], map: [{ match: "**", metadata: { family: "x" } }],
      document_policy: { delete_missing: true },
    });
  });
  it("leaves the ref out for the default branch and throws for a broken map", () => {
    const form = { repository: "o/r", ref: "", include: "**", exclude: "", map: "", deleteMissing: false };
    expect(repositoryConfig(form)).not.toHaveProperty("ref");
    expect(() => repositoryConfig({ ...form, map: "a: [" })).toThrow();
    expect(REPOSITORY_PATTERN.test("owner/name.js")).toBe(true);
    expect(REPOSITORY_PATTERN.test("owner")).toBe(false);
    expect(REPOSITORY_PATTERN.test("a/b/c")).toBe(false);
    expect(parseLines(" a \n\nb\na")).toEqual(["a", "b"]);
  });
});

describe("connections client", () => {
  it("maps API errors to their code text and names the sources of a connection in use", async () => {
    fetchMock.mockResolvedValue(json({ detail: "a source refers to this connection", code: "connection_in_use" }, 409));
    await expect(deleteConnection("c1")).rejects.toMatchObject({ status: 409, code: "connection_in_use" });
    fetchMock.mockResolvedValue(json({ detail: "name", code: "connection_name_conflict" }, 409));
    await expect(createConnection({ name: "ti", kind: "oauth2_client_credentials", config: { provider: "x" }, credential_refs: {}, enabled: true })).rejects.toMatchObject({ code: "connection_name_conflict" });
  });

  it("finds the sources using a connection across knowledge bases", async () => {
    fetchMock.mockImplementation(async (url: string) => {
      const p = String(url).replace(/^.*?\/api\/v1/, "");
      if (p === "/knowledge-bases") return json({ items: [{ id: "kb1", name: "Docs" }, { id: "kb2", name: "Parts" }], next_after: null });
      if (p === "/knowledge-bases/kb1/sources") return json({ items: [{ id: "s1", name: "a", connection_id: "c9" }] });
      if (p === "/knowledge-bases/kb2/sources") return json({ items: [{ id: "s2", name: "TI parts", connection_id: "c1" }] });
      return json({}, 404);
    });
    expect(await sourcesUsingConnection("c1")).toEqual([{ id: "s2", name: "TI parts", knowledgeBaseId: "kb2", knowledgeBaseName: "Parts" }]);
  });
});

describe("parsePartNumbers", () => {
  it("splits on whitespace, commas and semicolons, trims and de-duplicates", () => {
    expect(parsePartNumbers("A1, B2\nC3;A1  D4")).toEqual(["A1", "B2", "C3", "D4"]);
    expect(parsePartNumbers("  ")).toEqual([]);
  });
});

describe("syncInput", () => {
  it("targeted carries part numbers; incremental and full do not", () => {
    expect(syncInput("s1", "targeted", { partNumbers: ["A"], restart: true })).toEqual({ source_id: "s1", mode: "targeted", part_numbers: ["A"] });
    expect(syncInput("s1", "incremental", { partNumbers: ["A"], restart: true })).toEqual({ source_id: "s1", mode: "incremental" });
  });
  it("sends restart only for a full sync that asks for it", () => {
    expect(syncInput("s1", "full", { restart: true })).toEqual({ source_id: "s1", mode: "full", restart: true });
    expect(syncInput("s1", "full", { restart: false })).toEqual({ source_id: "s1", mode: "full" });
  });
});

describe("reportSummary", () => {
  it("reads the report, then the sync counters", () => {
    expect(reportSummary({ report: { discovered: 5, inserted: 2, updated: 1, unchanged: 2, documents_queued: 3, failed: 0, stop_reason: "budget" }, counters: {} })).toEqual({
      discovered: 5, inserted: 2, updated: 1, unchanged: 2, documents_queued: 3, failed: 0, stop_reason: "budget",
    });
    expect(reportSummary({ report: null, counters: { discovered: 4, failed: 1 } })).toMatchObject({ discovered: 4, failed: 1, inserted: 0, stop_reason: null });
  });
});

describe("the sync workflow", () => {
  const wf = (over = {}) => ({ id: "w1", name: SYNC_WORKFLOW_NAME, draft_spec: null, latest_version: 1, ...over });

  it("finds the published workflow by name", async () => {
    fetchMock.mockResolvedValue(json([{ id: "x", name: "Other", draft_spec: null, latest_version: 1 }, wf()]));
    expect((await findSyncWorkflow())?.id).toBe("w1");
  });
  it("ignores a workflow with no published version", async () => {
    fetchMock.mockResolvedValue(json([wf({ latest_version: null })]));
    expect(await findSyncWorkflow()).toBeNull();
  });

  it("publishes from the template route: create then publish", async () => {
    const spec = { spec_version: 1, name: SYNC_WORKFLOW_NAME };
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const p = String(url).replace(/^.*?\/api\/v1/, "");
      if (p === "/workflows" && !init?.method) return json([]);
      if (p === "/kb-sources/workflow-template") return json(spec);
      if (p === "/workflows" && init?.method === "POST") return json(wf({ latest_version: null }), 201);
      if (p === "/workflows/w1/versions") return json({ workflow_id: "w1", version: 1 }, 201);
      return json({}, 404);
    });
    const published = await publishSyncWorkflow();
    expect(published.id).toBe("w1");
    expect(published.latest_version).toBe(1);
    expect(urls()).toEqual(["GET /workflows", "GET /kb-sources/workflow-template", "POST /workflows", "POST /workflows/w1/versions"]);
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ spec });
  });

  it("publishes an existing unpublished draft instead of creating a second workflow", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const p = String(url).replace(/^.*?\/api\/v1/, "");
      if (p === "/workflows") return json([wf({ latest_version: null })]);
      if (p === "/workflows/w1/versions" && init?.method === "POST") return json({ version: 1 }, 201);
      return json({}, 404);
    });
    expect((await publishSyncWorkflow()).latest_version).toBe(1);
    expect(urls()).toEqual(["GET /workflows", "POST /workflows/w1/versions"]);
  });

  it("starts a sync through the published workflow with the sync input", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const p = String(url).replace(/^.*?\/api\/v1/, "");
      if (p === "/workflows") return json([wf()]);
      if (p === "/workflows/w1/runs") return json({ run_id: "r1", kind: "workflow", workflow_id: "w1", version: 1, status: "queued", display_status: "queued", trace_id: null }, 202);
      return json({}, 404);
    });
    const started = await startSourceSync("s1", "targeted", { partNumbers: ["A1"] });
    expect(started).toEqual({ workflowId: "w1", runId: "r1" });
    const body = JSON.parse(fetchMock.mock.calls[1][1].body);
    expect(body).toEqual({ input: { source_id: "s1", mode: "targeted", part_numbers: ["A1"] } });
  });
  it("refuses with a stable error when the workflow is not published", async () => {
    fetchMock.mockResolvedValue(json([]));
    await expect(startSourceSync("s1", "incremental", {})).rejects.toMatchObject({ code: "sync_workflow_missing" });
  });
});

describe("the adapter list", () => {
  beforeEach(() => resetAdapterCache());

  it("is fetched once per page load and a plug-in provider appears when the server lists it", async () => {
    fetchMock.mockResolvedValue(json({ items: [GITHUB, PLUGIN] }));
    const first = await listAdapters();
    await listAdapters();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/kb-sources/adapters");
    expect(first.map((item) => item.provider)).toEqual(["github", "acme"]);
    expect(providerInfo(first, "acme")?.kind).toBe("product_catalog");
    expect(providerLabel(first, "acme")).toBe("Acme Parts");
    expect(providerLabel(first, "unlisted")).toBe("unlisted");
  });

  it("retries after a failed fetch", async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: "x" }, 500));
    await expect(listAdapters()).rejects.toBeDefined();
    fetchMock.mockResolvedValueOnce(json({ items: [GITHUB] }));
    expect(await listAdapters()).toHaveLength(1);
  });
});
