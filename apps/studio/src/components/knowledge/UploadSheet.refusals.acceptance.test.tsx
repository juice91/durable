// Acceptance (browser check C/D/F): the upload Sheet sniffs binary content under a text name (PDF, PNG,
// JPEG, WebP at offset 0 only), labels each refusal's chip, and lets refused or failed files be removed.
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { detectBinaryType } from "@/lib/knowledge";
import { UploadSheet } from "./UploadSheet";

let puts: Array<{ path: string; body: Record<string, unknown> }>;
let putStatus: (path: string) => number;

beforeEach(() => {
  puts = [];
  putStatus = () => 200;
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit = {}) => {
    const path = new URL(input).pathname.replace(/^\/api\/v1/, "");
    if ((init.method ?? "GET").toUpperCase() === "PUT") {
      puts.push({ path, body: JSON.parse(String(init.body)) });
      const status = putStatus(path);
      const body = status >= 400 ? { detail: "boom" } : { document: { id: "d1", latest_version: 1 }, run_id: "r1" };
      return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
    }
    return new Response(JSON.stringify({ items: [] }), { status: 200, headers: { "content-type": "application/json" } });
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const bytes = (...b: number[]) => new Uint8Array(b);
const PDF = [0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x37, 0x0a];
const PNG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0];
const JPEG = [0xff, 0xd8, 0xff, 0xe0, 0, 0x10];
const WEBP = [0x52, 0x49, 0x46, 0x46, 1, 2, 3, 4, 0x57, 0x45, 0x42, 0x50];

function open() {
  render(<UploadSheet open onOpenChange={() => undefined} kbId="kb1" kbName="Acme" onUploaded={() => undefined} />);
  return screen.getByTestId("knowledge-upload-sheet");
}
const drop = (sheet: HTMLElement, files: File[]) =>
  fireEvent.drop(within(sheet).getByTestId("knowledge-upload-dropzone"), { dataTransfer: { files } });
const rows = (sheet: HTMLElement) => within(sheet).queryAllByTestId("knowledge-upload-file");
const submit = (sheet: HTMLElement) => within(sheet).getByTestId("knowledge-upload-submit");

describe("detectBinaryType (mirror of the API's sniff)", () => {
  it("names pdf, png, jpeg and webp at offset 0 and nothing else", () => {
    expect(detectBinaryType(bytes(...PDF))).toBe("application/pdf");
    expect(detectBinaryType(bytes(...PNG))).toBe("image/png");
    expect(detectBinaryType(bytes(...JPEG))).toBe("image/jpeg");
    expect(detectBinaryType(bytes(...WEBP))).toBe("image/webp");
    expect(detectBinaryType(new TextEncoder().encode("see %PDF-1.7 spec"))).toBeNull();
    expect(detectBinaryType(new TextEncoder().encode(" %PDF-1.7"))).toBeNull();
    expect(detectBinaryType(bytes(0x52, 0x49, 0x46, 0x46, 1, 2, 3, 4, 0x57, 0x41, 0x56, 0x45))).toBeNull(); // RIFF WAVE
    expect(detectBinaryType(bytes())).toBeNull();
  });
});

describe("UploadSheet refusals", () => {
  it("refuses JPEG and WebP under text names with what they look like, sending nothing", async () => {
    const sheet = open();
    drop(sheet, [new File([bytes(...JPEG)], "p.md"), new File([bytes(...WEBP)], "q.html")]);
    fireEvent.click(submit(sheet));
    expect(await within(sheet).findByText(/It looks like image\/jpeg, not text\/markdown\./)).toBeInTheDocument();
    expect(await within(sheet).findByText(/It looks like image\/webp, not text\/html\./)).toBeInTheDocument();
    expect(rows(sheet).map((r) => r.getAttribute("data-status"))).toEqual(["refused", "refused"]);
    expect(puts).toHaveLength(0);
  });

  it("text that merely mentions %PDF- uploads, and a real .pdf is sent as base64", async () => {
    const sheet = open();
    drop(sheet, [new File(["notes: files start with %PDF-1.7\n"], "n.txt"), new File([bytes(...PDF)], "r.pdf")]);
    fireEvent.click(submit(sheet));
    await waitFor(() => expect(puts).toHaveLength(2));
    const bodies = Object.fromEntries(puts.map((p) => [p.path.split("/").pop(), p.body]));
    expect(bodies["n.txt"]).toMatchObject({ content: "notes: files start with %PDF-1.7\n" });
    expect(bodies["r.pdf"]).toHaveProperty("content_base64");
  });

  it("labels each refusal chip: Not supported, Empty, Too large, Can't read", async () => {
    const sheet = open();
    const big = new File(["x"], "big.md");
    Object.defineProperty(big, "size", { value: 6 * 1024 * 1024 });
    drop(sheet, [new File(["x"], "a.docx"), new File([], "e.md"), big, new File([bytes(0x23, 0x20, 0xff, 0xfe)], "l.md")]);
    expect(rows(sheet).map((r) => r.getAttribute("data-status"))).toEqual(["refused", "refused", "refused", "ready"]);
    fireEvent.click(submit(sheet));
    await waitFor(() => expect(within(rows(sheet)[3]).getByText("Can't read")).toBeInTheDocument());
    const chips = rows(sheet).map((r) => ["Not supported", "Empty", "Too large", "Can't read"].filter((c) => within(r).queryByText(c)));
    expect(chips).toEqual([["Not supported"], ["Empty"], ["Too large"], ["Can't read"]]);
    expect(within(rows(sheet)[2]).queryByText("Not supported")).toBeNull();
  });

  it("every refused row can be removed, leaving the rest", async () => {
    const sheet = open();
    drop(sheet, [new File(["x"], "a.docx"), new File([], "e.md"), new File(["ok"], "good.md")]);
    expect(submit(sheet)).toHaveTextContent("Upload 1 file");
    for (const name of ["a.docx", "e.md"]) fireEvent.click(within(sheet).getByRole("button", { name: `Remove ${name}` }));
    expect(rows(sheet)).toHaveLength(1);
    fireEvent.click(submit(sheet));
    await waitFor(() => expect(puts).toHaveLength(1));
  });

  it("a failed upload can be removed and is not re-sent; the retry sends only what remains", async () => {
    putStatus = (path) => (path.endsWith("/bad.md") ? 500 : 200);
    const sheet = open();
    drop(sheet, [new File(["a"], "bad.md"), new File(["b"], "other.md")]);
    fireEvent.click(submit(sheet));
    await waitFor(() => expect(rows(sheet).map((r) => r.getAttribute("data-status"))).toEqual(["failed", "started"]));
    const before = puts.length;
    fireEvent.click(within(sheet).getByRole("button", { name: "Remove bad.md" }));
    expect(rows(sheet)).toHaveLength(1);
    expect(puts).toHaveLength(before);
    expect(submit(sheet)).toBeDisabled();
  });
});
