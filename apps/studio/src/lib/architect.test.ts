import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { ArchitectError, draftAgent, formToSpec, saveAgent, seedArchitect, specToForm, EMPTY_FORM } from "./architect";

beforeEach(() => vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "t"));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const reply = (status: number, body: unknown) => vi.fn(async () => new Response(JSON.stringify(body), { status }));

describe("lib/architect", () => {
  test("formToSpec leaves unset optional fields out and keeps numbers numeric", () => {
    const spec = formToSpec({ ...EMPTY_FORM, name: " Bot ", instructions: "Do it", model: "", temperature: "0.5", max_tokens: "" });
    expect(spec).toEqual({ name: "Bot", description: "", instructions: "Do it", tools: [], mcp_tools: [], knowledge_bases: [], memory: false, model_settings: { temperature: 0.5 } });
  });

  test("specToForm and formToSpec round-trip", () => {
    const spec = { name: "A", id: "a-bot", description: "d", instructions: "i", model: "p:m", tools: ["x"], mcp_tools: [{ server: "s", tool: "t" }], knowledge_bases: ["k"], memory: true, model_settings: { temperature: 0.1, max_tokens: 9 } };
    expect(formToSpec(specToForm(spec))).toEqual(spec);
  });

  test("errors carry the API's code and fields", async () => {
    vi.stubGlobal("fetch", reply(409, { detail: "stale", code: "version_mismatch", latest_version: 4 }));
    const error = await saveAgent(formToSpec({ ...EMPTY_FORM, name: "n", instructions: "i" }), { publish: false }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ArchitectError);
    expect(error).toMatchObject({ status: 409, code: "version_mismatch", message: "stale", extra: { latest_version: 4 } });
  });

  test("draftAgent sends current only when given; seedArchitect returns the choose_model refusal as an error", async () => {
    const fetchMock = reply(200, { spec: {}, assumptions: [], dropped: [], run_id: "r", model: "m" });
    vi.stubGlobal("fetch", fetchMock);
    await draftAgent("hello");
    expect(JSON.parse(String((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].body))).toEqual({ description: "hello" });
    vi.stubGlobal("fetch", reply(409, { detail: "Choose", code: "choose_model", providers: [] }));
    await expect(seedArchitect()).rejects.toMatchObject({ code: "choose_model" });
  });
});
