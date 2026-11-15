import { describe, expect, it } from "vitest";
import { describeDocumentError, emptyExtractionText, EXTRACTION_ERROR_MESSAGES } from "@/lib/knowledge";

describe("empty extraction (RAG P1 quality fixes)", () => {
  it("emptyExtractionText is a note only for no_extractable_text", () => {
    expect(emptyExtractionText({ empty_reason: "no_extractable_text" })).toMatch(/^No extractable text/);
    expect(emptyExtractionText({ empty_reason: null })).toBeNull();
    expect(emptyExtractionText({})).toBeNull();
    expect(emptyExtractionText(null)).toBeNull();
    expect(emptyExtractionText(undefined)).toBeNull();
  });

  it("emptyExtractionText gives the reason for the document's own extractor", () => {
    const note = (extractor: string) => emptyExtractionText({ empty_reason: "no_extractable_text", extractor });
    expect(note("rst")).toMatch(/toctree or include directives\.$/);
    expect(note("yaml")).toBe("No extractable text: nothing is indexed. Expected for a YAML file with no values, or only a licence header.");
    expect(note("json")).toBe("No extractable text: nothing is indexed. Expected for a JSON file with no values.");
    expect(note("markdown-doxygen")).toMatch(/Doxygen commands/);
    for (const extractor of ["yaml", "json", "markdown-doxygen"]) expect(note(extractor)).not.toMatch(/toctree/);
    expect(note("something-new")).toBe("No extractable text: nothing is indexed.");
  });

  it("no_text_layer has its own sentence, distinct from no_extractable_text", () => {
    expect(EXTRACTION_ERROR_MESSAGES.no_text_layer).toMatch(/^Image-only PDF with no text layer/);
    expect(EXTRACTION_ERROR_MESSAGES.no_text_layer).not.toEqual(EXTRACTION_ERROR_MESSAGES.no_extractable_text);
    expect(EXTRACTION_ERROR_MESSAGES.no_extractable_text).toBe("No text could be extracted from the document.");
  });

  it("describeDocumentError shows the no_text_layer message for a failed scan", () => {
    const text = describeDocumentError({
      status: "failed", error: "extraction_failed: x", error_code: "extraction_failed", error_reason: "no_text_layer",
    } as never);
    expect(JSON.stringify(text)).toContain("Image-only PDF with no text layer");
  });
});
