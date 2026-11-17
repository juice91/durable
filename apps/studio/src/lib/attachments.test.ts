import { describe, expect, it } from "vitest";
import { acceptFor, attachmentNotice, attachmentRefusal, type ModelCapabilities } from "@/lib/attachments";

const pdf = { name: "d.pdf", type: "application/pdf", size: 5 };
const png = { name: "a.png", type: "image/png", size: 5 };
const note = { name: "n.md", type: "", size: 5 };
const both: ModelCapabilities = { image: true, pdf: true, source: "gateway" };
const noPdf: ModelCapabilities = { image: true, pdf: false, source: "gateway" };
const textOnly: ModelCapabilities = { image: false, pdf: false, source: "static" };
const guessNoPdf: ModelCapabilities = { image: true, pdf: false, source: "assumed" };

describe("acceptFor", () => {
  it("lists everything without capabilities (a free-typed model)", () => {
    for (const none of [undefined, null]) {
      const accept = acceptFor(none);
      expect(accept).toContain("application/pdf");
      expect(accept).toContain("image/png");
    }
  });

  it("leaves out PDF when pdf is false and the images when image is false, never the text types", () => {
    expect(acceptFor(noPdf)).not.toContain("application/pdf");
    expect(acceptFor(noPdf)).toContain("image/webp");
    const text = acceptFor(textOnly);
    expect(text).not.toContain("image/");
    expect(text).not.toContain("application/pdf");
    for (const kept of ["text/plain", "text/markdown", "text/csv", "application/json", ".md", ".csv", ".txt", ".json"]) {
      expect(text).toContain(kept);
    }
    expect(acceptFor(both)).toBe(acceptFor(null));
  });

  it("ignores an assumed 'no': a guess must not hide a file type, and the upload is allowed", () => {
    expect(acceptFor(guessNoPdf)).toBe(acceptFor(null));
    expect(attachmentRefusal(pdf, guessNoPdf)).toBeNull();
  });
});

describe("refusal and notice copy", () => {
  it("refuses a file the model does not take when the capability is a gateway or static fact", () => {
    expect(attachmentRefusal(pdf, noPdf)).toBe("This model does not accept PDF files.");
    expect(attachmentRefusal(png, textOnly)).toBe("This model does not accept image files.");
    expect(attachmentNotice(pdf, noPdf)).toBeNull();
  });

  it("only warns, and allows the upload, when the capability is assumed", () => {
    expect(attachmentRefusal(pdf, guessNoPdf)).toBeNull();
    expect(attachmentNotice(pdf, guessNoPdf)).toBe("This model may not accept PDF files.");
  });

  it("never refuses or warns about text, or with no capabilities, or when the model takes the file", () => {
    expect(attachmentRefusal(note, textOnly)).toBeNull();
    expect(attachmentNotice(note, guessNoPdf)).toBeNull();
    expect(attachmentRefusal(pdf, null)).toBeNull();
    expect(attachmentRefusal(pdf, both)).toBeNull();
    expect(attachmentNotice(png, guessNoPdf)).toBeNull();
  });

  it("keeps the type and size rules first", () => {
    expect(attachmentRefusal({ name: "x.exe", type: "application/x-msdownload", size: 5 }, noPdf)).toMatch(/not a supported file type/);
  });
});
