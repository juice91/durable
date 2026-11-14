import { seg, withQuery } from "./query";

/** The knowledge base page's tabs (RM.16). Documents is the default and is left out of URLs. */
export const KNOWLEDGE_TABS = ["documents", "search", "runs"] as const;
export type KnowledgeTab = (typeof KNOWLEDGE_TABS)[number];
/** The detail page's own tab parameter (`/knowledge/:id?tab=search`). */
export const KNOWLEDGE_TAB_PARAM = "tab";
/** The open document Sheet's document key (`/knowledge/:id?doc=<key>`). */
export const KNOWLEDGE_DOC_PARAM = "doc";
/** The trace Sheet's tab parameter on the knowledge base page (`tab` is the page's own). */
export const KNOWLEDGE_TRACE_TAB_PARAM = "traceTab";

/** A URL's `tab` value as a knowledge base tab; anything else (or nothing) is `documents`. */
export function parseKnowledgeTab(raw: string | null | undefined): KnowledgeTab {
  return (KNOWLEDGE_TABS as readonly string[]).includes(raw ?? "") ? (raw as KnowledgeTab) : "documents";
}

export interface KnowledgeBaseRouteOptions {
  /** `documents` (default, left out), `search` or `runs`. */
  tab?: KnowledgeTab | null;
  /** A document key: opens that document's Sheet (on the Documents tab). */
  document?: string | null;
}

export const knowledgeRoutes = {
  /** `/knowledge`: the knowledge bases list. */
  knowledge: (): string => "/knowledge",
  /** `/knowledge/:id[?tab=search|runs][&doc=<document key>]`: one knowledge base. */
  knowledgeBase: (id: string, { tab = null, document = null }: KnowledgeBaseRouteOptions = {}): string =>
    withQuery(`/knowledge/${seg(id)}`, {
      [KNOWLEDGE_TAB_PARAM]: tab && tab !== "documents" ? tab : null,
      [KNOWLEDGE_DOC_PARAM]: document || null,
    }),
};
