import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, ConflictError, apiErrorFromResponse } from "./api";
import { forgetRevisions, ifMatch, isConflict, recallRevision, revisionFromEtag, revisionKey, track, trackAll, writeResource } from "./revisions";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("revisions", () => {
  beforeEach(() => forgetRevisions());

  it("remembers the revision of what it reads and sends it as a quoted If-Match", () => {
    expect(ifMatch("agent:a1")).toEqual({});
    track(revisionKey.agent("a1"), { id: "a1", revision: 3 });
    expect(recallRevision("agent:a1")).toBe(3);
    expect(ifMatch("agent:a1")).toEqual({ "If-Match": '"3"' });
    trackAll([{ id: "s1", revision: 7 }, { id: "s2" }], (s) => revisionKey.schedule(s.id));
    expect(ifMatch("schedule:s1")).toEqual({ "If-Match": '"7"' });
    expect(ifMatch("schedule:s2")).toEqual({}); // an item without a revision (an older API) sends nothing
  });

  it("parses ETag values", () => {
    expect(revisionFromEtag('"4"')).toBe(4);
    expect(revisionFromEtag('W/"4"')).toBe(4);
    expect(revisionFromEtag("4")).toBe(4);
    expect(revisionFromEtag("abc")).toBeUndefined();
    expect(revisionFromEtag(null)).toBeUndefined();
  });

  it("a write sends the remembered revision and remembers the saved one", async () => {
    track(revisionKey.scorer("c1"), { revision: 2 });
    const send = vi.fn(async (_headers: Record<string, string>) => ({ id: "c1", revision: 3 }));
    await writeResource(revisionKey.scorer("c1"), send);
    expect(send).toHaveBeenCalledWith({ "If-Match": '"2"' });
    expect(recallRevision("scorer:c1")).toBe(3);
  });

  it("a 412 becomes a ConflictError carrying the current resource and the key it used", async () => {
    const error = await apiErrorFromResponse(
      json(412, { detail: "changed", code: "revision_mismatch", current_revision: 5, current: { id: "w1", revision: 5 } }),
    );
    expect(error).toBeInstanceOf(ConflictError);
    expect(error).toBeInstanceOf(ApiError);
    const conflict = error as ConflictError;
    expect(conflict.currentRevision).toBe(5);
    expect(conflict.current).toEqual({ id: "w1", revision: 5 });
    await expect(writeResource(revisionKey.workflow("w1"), async () => { throw conflict; })).rejects.toMatchObject({ key: "workflow:w1" });
    expect(isConflict(conflict)).toBe(true);
    expect(isConflict(new ApiError(409, "x"))).toBe(false);
  });

  it("a 412 without the revision_mismatch code stays a plain ApiError", async () => {
    expect(await apiErrorFromResponse(json(412, { detail: "no" }))).not.toBeInstanceOf(ConflictError);
  });
});
