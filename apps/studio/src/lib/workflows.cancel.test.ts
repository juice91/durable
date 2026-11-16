// LP.12 review: cancelRun against a stubbed fetch (the run view tests mock it).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, BASE_URL } from "./api";
import { cancelRun } from "./workflows";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("cancelRun", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("POSTs /api/v1/runs/{id}/cancel with no body and returns the 202 answer", async () => {
    const answer = { run_id: "r/1", kind: "workflow", status: "failed", display_status: "failed", error_code: "run_cancelled", cancelled: true, requested_by: "ada", approvals_closed: 1 };
    fetchMock.mockResolvedValueOnce(json(answer, 202));
    await expect(cancelRun("r/1")).resolves.toEqual(answer);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE_URL}/api/v1/runs/r%2F1/cancel`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
    expect(init.headers).toMatchObject({ Authorization: "Bearer test" });
  });

  it.each([
    [409, { detail: "the run has already ended" }, "the run has already ended"],
    [409, { detail: "only workflow runs can be cancelled", code: "cancel_not_supported" }, "only workflow runs can be cancelled"],
    [404, { detail: "Run not found" }, "Run not found"],
    [503, { detail: "cancel recorded but not finished; it completes automatically, or retry the request" }, "cancel recorded but not finished"],
  ])("surfaces a %i's detail as an ApiError", async (status, body, message) => {
    fetchMock.mockResolvedValueOnce(json(body, status));
    const error = await cancelRun("r1").catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(status);
    expect((error as ApiError).message).toContain(message);
  });
});
