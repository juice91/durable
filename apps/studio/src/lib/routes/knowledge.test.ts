import { describe, expect, it } from "vitest";
import { routes } from "./index";
import { parseKnowledgeTab } from "./knowledge";

describe("knowledge routes (RM.16)", () => {
  it("builds the list and detail URLs; documents (the default tab) stays out of the URL", () => {
    expect(routes.knowledge()).toBe("/knowledge");
    expect(routes.knowledgeBase("kb1")).toBe("/knowledge/kb1");
    expect(routes.knowledgeBase("kb1", { tab: "documents" })).toBe("/knowledge/kb1");
    expect(routes.knowledgeBase("kb1", { tab: "search" })).toBe("/knowledge/kb1?tab=search");
    expect(routes.knowledgeBase("kb1", { tab: "runs" })).toBe("/knowledge/kb1?tab=runs");
  });

  it("encodes the id and the document key (keys may hold spaces, ?, # and &)", () => {
    expect(routes.knowledgeBase("a/b")).toBe("/knowledge/a%2Fb");
    const href = routes.knowledgeBase("kb1", { document: "FAQ & more?.md#x" });
    expect(new URL(href, "http://s").searchParams.get("doc")).toBe("FAQ & more?.md#x");
    expect(() => routes.knowledgeBase("..")).toThrow();
  });

  it("parseKnowledgeTab falls back to documents", () => {
    expect(parseKnowledgeTab("search")).toBe("search");
    expect(parseKnowledgeTab("runs")).toBe("runs");
    expect(parseKnowledgeTab("nope")).toBe("documents");
    expect(parseKnowledgeTab(null)).toBe("documents");
  });
});
