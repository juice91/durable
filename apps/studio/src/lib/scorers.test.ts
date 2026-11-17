import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ScoreRunError, scoreRun } from "./scorers";

const reply = (status: number, body: unknown) => vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })));

beforeEach(() => vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token"));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("scoreRun", () => {
  it("posts scorer_ids and resolves on 202, partial or not", async () => {
    reply(202, { run_id: "r1", scorer_ids: ["s1"], already_scored: ["s2"] });
    await expect(scoreRun("r1", ["s1", "s2"])).resolves.toBeUndefined();
    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/api\/v1\/runs\/r1\/score$/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ scorer_ids: ["s1", "s2"] });
  });

  it("maps 409 run_not_finished to a readable message", async () => {
    reply(409, { detail: "run_not_finished" });
    const error = await scoreRun("r1", ["s1"]).catch((e) => e);
    expect(error).toBeInstanceOf(ScoreRunError);
    expect(error.code).toBe("run_not_finished");
    expect(error.message).toMatch(/only completed runs can be scored/i);
  });

  it("maps 409 already_scored (detail string or object, with ids) to a readable message", async () => {
    reply(409, { detail: "already_scored", scorer_ids: ["s1"] });
    let error = await scoreRun("r1", ["s1"]).catch((e) => e);
    expect(error.code).toBe("already_scored");
    expect(error.message).toMatch(/already scored/i);
    reply(409, { detail: { error: "already_scored", scorer_ids: ["s1"] } });
    error = await scoreRun("r1", ["s1"]).catch((e) => e);
    expect(error.code).toBe("already_scored");
  });

  it("reads the API's real already_scored 409 body", async () => {
    reply(409, { detail: "already_scored", already_scored: ["s1", "s2"] });
    const error = await scoreRun("r1", ["s1", "s2"]).catch((e) => e);
    expect(error).toBeInstanceOf(ScoreRunError);
    expect(error.code).toBe("already_scored");
  });

  it("leaves other failures as the API's message", async () => {
    reply(404, { detail: "scorer not found" });
    await expect(scoreRun("r1", ["s1"])).rejects.toThrow("scorer not found");
  });
});
