import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Attachment } from "@/lib/attachments";
import { createAttachmentAdapter } from "./attachmentAdapter";

const ID = "11111111-1111-4111-8111-111111111111";
const file = new File([new Uint8Array(8)], "pic.png", { type: "image/png" });
const upload = async (f: File): Promise<Attachment> => ({ id: ID, name: f.name, media_type: f.type, size: f.size });

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

async function added(adapter: ReturnType<typeof createAttachmentAdapter>) {
  let last: unknown;
  const add = adapter.add({ file } as never) as AsyncGenerator<unknown>;
  for await (const item of add) last = item;
  return last as { id: string };
}

const deletes = () => fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "DELETE");

describe("removing a chip discards its upload", () => {
  it("calls DELETE once with the uploaded id", async () => {
    const adapter = createAttachmentAdapter(upload);
    const chip = await added(adapter);
    await adapter.remove(chip as never);
    await adapter.remove(chip as never); // a second remove has nothing left to discard
    expect(deletes()).toHaveLength(1);
    expect(String(deletes()[0][0])).toContain(`/api/v1/attachments/${ID}`);
  });

  it("ignores a failed delete", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    const adapter = createAttachmentAdapter(upload);
    const chip = await added(adapter);
    await expect(adapter.remove(chip as never)).resolves.toBeUndefined();
    await Promise.resolve();
    expect(deletes()).toHaveLength(1);
  });

  it("does not discard an attachment that was sent", async () => {
    const adapter = createAttachmentAdapter(upload);
    const chip = await added(adapter);
    await adapter.send(chip as never);
    expect(deletes()).toHaveLength(0);
  });
});
