import { afterEach, describe, expect, it, vi } from "vitest";
import { copyText } from "./clipboard";

const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
afterEach(() => {
  if (original) Object.defineProperty(navigator, "clipboard", original);
  else delete (navigator as { clipboard?: unknown }).clipboard;
});

const setClipboard = (value: unknown) => Object.defineProperty(navigator, "clipboard", { configurable: true, value });

describe("copyText", () => {
  it("writes the text and resolves true", async () => {
    const writeText = vi.fn(async () => undefined);
    setClipboard({ writeText });
    await expect(copyText("run-1")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("run-1");
  });

  it("resolves false (never throws) when the write is refused or there is no clipboard", async () => {
    setClipboard({ writeText: vi.fn(async () => { throw new Error("NotAllowedError"); }) });
    await expect(copyText("x")).resolves.toBe(false);
    setClipboard(undefined);
    await expect(copyText("x")).resolves.toBe(false);
  });
});
