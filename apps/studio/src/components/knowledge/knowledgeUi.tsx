"use client";

import React from "react";
import { Badge, type BadgeProps } from "@/components/ui/Badge";
import type { LinkTab } from "@/components/ui/Tabs";
import { formatCost } from "@/lib/format";
import {
  DOCUMENT_STATUS_LABELS,
  KNOWLEDGE_BASE_STATUS_LABELS,
  type DocumentStatus,
  type KnowledgeBaseStatus,
} from "@/lib/knowledge";
import { routes } from "@/lib/routes";
import type { KnowledgeTab } from "@/lib/routes/knowledge";

/** Inputs, selects and textareas on the Knowledge pages (theme tokens only). */
export const fieldClass =
  "w-full min-w-0 rounded-md border border-input bg-surface px-3 py-1.5 text-label text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
export const labelClass = "block text-label font-medium text-foreground";
export const hintClass = "text-caption text-muted-foreground";
export const alertClass = "rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text";
export const noticeClass = "rounded-md border border-border bg-muted/40 p-3 text-body text-foreground";

const DOCUMENT_VARIANT: Record<DocumentStatus, BadgeProps["variant"]> = {
  pending: "neutral",
  ingesting: "info",
  ready: "success",
  failed: "destructive",
};

const KB_VARIANT: Record<KnowledgeBaseStatus, BadgeProps["variant"]> = {
  ready: "success",
  reindexing: "info",
  reindex_failed: "destructive",
};

export function DocumentStatusBadge({ status }: { status: DocumentStatus }) {
  return (
    <Badge variant={DOCUMENT_VARIANT[status] ?? "neutral"} data-testid="knowledge-document-status" data-status={status}>
      {DOCUMENT_STATUS_LABELS[status] ?? status}
    </Badge>
  );
}

export function KnowledgeBaseStatusBadge({ status }: { status: KnowledgeBaseStatus }) {
  return (
    <Badge variant={KB_VARIANT[status] ?? "neutral"} data-testid="knowledge-base-status" data-status={status}>
      {KNOWLEDGE_BASE_STATUS_LABELS[status] ?? status}
    </Badge>
  );
}

/** "$0.0012", "No price" when some request was unpriced (null), or "—" when not loaded. */
export function ingestCostLabel(cost: string | null | undefined, loaded: boolean): string {
  if (!loaded) return "—";
  return formatCost(cost) ?? "No price";
}

/** A date-time in the viewer's locale; the raw value when it does not parse. */
export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/** The knowledge base page's tabs: Documents · Search · Ingest runs. */
export function knowledgeTabs(kbId: string, active: KnowledgeTab): LinkTab[] {
  return [
    { href: routes.knowledgeBase(kbId), label: "Documents", active: active === "documents", testId: "knowledge-tab-documents" },
    { href: routes.knowledgeBase(kbId, { tab: "search" }), label: "Search", active: active === "search", testId: "knowledge-tab-search" },
    { href: routes.knowledgeBase(kbId, { tab: "runs" }), label: "Ingest runs", active: active === "runs", testId: "knowledge-tab-runs" },
  ];
}
