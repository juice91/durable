"use client";

import React, { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { listKnowledgeBases, type KnowledgeBase } from "@/lib/knowledge";
import { routes } from "@/lib/routes";
import { Button } from "@/components/ui/Button";
import {
  isKnowledgeError,
  KB_DEFAULT_TOP_K,
  KB_MAX_TOP_K,
  knowledgeBasesOf,
  knowledgeBasesValue,
  knowledgeProblems,
  knowledgeStatus,
  KNOWLEDGE_SPEC_KEYS,
  MAX_KNOWLEDGE_BASES_PER_AGENT,
  newKnowledgeBaseRef,
  type KnowledgeBaseRefSpec,
} from "../knowledgeMemory";
import { groupClass, hintClass, JsonTextarea, legendClass, NumberField, problemClass, useShownOnce } from "./knowledgeMemoryFields";
import type { EditorStepDefinition, MissingField, StepProps } from "./types";

/** Element id of an attachment's field (`kb-<index>-<field>`), matching `knowledgeProblems` fields. */
const fieldId = (field: string) => `kb-${field}`;

/**
 * Step 6: the knowledge bases the agent retrieves from (`spec.knowledge_bases`), each with top K,
 * a minimum score and a metadata filter. Attaching any gives the agent a `retrieve` tool.
 */
export function KnowledgeStep({ spec, onSpecChange }: StepProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const refs = knowledgeBasesOf(spec);
  // The list is loaded the first time the step is shown (or at once when the spec attaches some,
  // so their names show).
  const shown = useShownOnce(rootRef, refs.length > 0);
  const [bases, setBases] = useState<KnowledgeBase[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!shown) return;
    let cancelled = false;
    Promise.resolve()
      .then(() => listKnowledgeBases())
      .then((items) => {
        if (!cancelled) setBases(items ?? []);
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadError(error instanceof Error ? error.message : "Failed to load knowledge bases");
      });
    return () => {
      cancelled = true;
    };
  }, [shown]);

  const problems = knowledgeProblems(spec);
  const problemFor = (field: string) => problems.find((p) => p.field === field)?.message ?? null;
  const write = (next: KnowledgeBaseRefSpec[]) => onSpecChange({ knowledge_bases: knowledgeBasesValue(next) });
  const update = (index: number, patch: Partial<KnowledgeBaseRefSpec>) => write(refs.map((ref, i) => (i === index ? { ...ref, ...patch } : ref)));
  const attachedIds = new Set(refs.map((ref) => ref.id));
  const full = refs.length >= MAX_KNOWLEDGE_BASES_PER_AGENT;
  const toggle = (id: string) => (attachedIds.has(id) ? write(refs.filter((ref) => ref.id !== id)) : write([...refs, newKnowledgeBaseRef(id)]));
  const byId = new Map((bases ?? []).map((kb) => [kb.id, kb]));

  return (
    <div ref={rootRef} className="min-w-0 space-y-6">
      <p className="text-label text-muted-foreground">
        Attached knowledge bases give the agent a <code className="font-mono">retrieve</code> tool. Its answers cite the sections it used, with their titles and links.
      </p>

      <fieldset className="min-w-0">
        <legend className="mb-2 text-label font-medium text-foreground">Knowledge bases</legend>
        {loadError ? (
          <p role="alert" className={problemClass}>
            Could not load knowledge bases: {loadError}
          </p>
        ) : bases === null ? (
          <p role="status" className="text-caption text-muted-foreground">
            Loading knowledge bases…
          </p>
        ) : bases.length === 0 ? (
          <p data-testid="knowledge-empty" className="text-label text-muted-foreground">
            No knowledge bases yet.{" "}
            <Link href={routes.knowledge()} className="text-foreground underline underline-offset-2">
              Create one on the Knowledge page
            </Link>
            .
          </p>
        ) : (
          <ul className="min-w-0 space-y-2">
            {bases.map((kb, kbIndex) => {
              const attached = attachedIds.has(kb.id);
              const disabled = !attached && full;
              const descriptionId = `kb-option-${kb.id}-description`;
              return (
                <li key={kb.id} className="min-w-0">
                  <div className={`min-w-0 rounded-md border border-border bg-surface px-3 py-2 ${disabled ? "opacity-60" : "hover:bg-fill-hover"}`}>
                    <label className={`flex min-w-0 items-start gap-2 ${disabled ? "cursor-not-allowed" : "cursor-pointer"}`}>
                      <input
                        // The first option is the list's focus target ("at most 8" problem: `kb-list`).
                        id={kbIndex === 0 ? fieldId("list") : undefined}
                        type="checkbox"
                        className="mt-0.5 size-4 shrink-0 accent-primary"
                        checked={attached}
                        disabled={disabled}
                        onChange={() => toggle(kb.id)}
                        aria-describedby={descriptionId}
                      />
                      <span className="min-w-0 flex-1 break-words text-label font-medium text-foreground">{kb.name}</span>
                    </label>
                    <p id={descriptionId} className="ml-6 break-words text-caption text-muted-foreground">
                      {[
                        kb.description,
                        typeof kb.document_count === "number" ? `${kb.document_count} ${kb.document_count === 1 ? "document" : "documents"}` : null,
                        kb.embedding_model,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
        <p className={hintClass} aria-live="polite" data-testid="knowledge-count">
          {refs.length} attached (max {MAX_KNOWLEDGE_BASES_PER_AGENT})
        </p>
        {problemFor("list") ? <p className={problemClass}>{problemFor("list")}</p> : null}
      </fieldset>

      {refs.map((ref, index) => {
        const kb = byId.get(ref.id);
        const unknown = bases !== null && !kb;
        const titleId = `kb-${index}-title`;
        const name = kb?.name ?? (bases === null ? ref.id : "Unknown knowledge base");
        return (
          <section key={`${ref.id}-${index}`} aria-labelledby={titleId} data-testid="knowledge-ref" className={groupClass}>
            <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <h3 id={titleId} className={`${legendClass} break-words px-0`}>
                  {name}
                </h3>
                <p className="break-all font-mono text-caption text-muted-foreground">{ref.id}</p>
              </div>
              {/* The attachment's id problems (not a UUID, attached twice) focus its Detach button. */}
              <Button id={fieldId(`${index}-id`)} size="sm" variant="ghost" onClick={() => write(refs.filter((_, i) => i !== index))} aria-label={`Detach ${name}`}>
                Detach
              </Button>
            </div>
            {unknown ? (
              <p className={problemClass}>This knowledge base does not exist in this tenant (deleted?). Publishing will fail until it is detached.</p>
            ) : null}
            {problemFor(`${index}-id`) ? <p className={problemClass}>{problemFor(`${index}-id`)}</p> : null}
            <div className="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2">
              <NumberField
                id={fieldId(`${index}-top_k`)}
                label={`Top K (1–${KB_MAX_TOP_K})`}
                value={ref.top_k}
                min={1}
                max={KB_MAX_TOP_K}
                placeholder={`${KB_DEFAULT_TOP_K} (default)`}
                hint="Sections returned per search."
                defaultValue={KB_DEFAULT_TOP_K}
                onChange={(value) => update(index, { top_k: value })}
                problem={problemFor(`${index}-top_k`)}
              />
              <NumberField
                id={fieldId(`${index}-min_score`)}
                label="Minimum score (0–1, optional)"
                value={ref.min_score}
                min={0}
                max={1}
                step={0.05}
                placeholder="No minimum"
                hint="Sections scoring lower are dropped."
                onChange={(value) => update(index, { min_score: value })}
                problem={problemFor(`${index}-min_score`)}
              />
            </div>
            <JsonTextarea
              key={`filter-${ref.id}`}
              id={fieldId(`${index}-filter`)}
              label="Metadata filter (optional)"
              rows={3}
              value={ref.filter}
              placeholder='{"lang": "en", "tag": ["faq", "guide"]}'
              hint="Only documents whose metadata matches: a value means equals, a list means any of."
              onChange={(value) => update(index, { filter: isEmptyObject(value) ? undefined : value })}
              problem={problemFor(`${index}-filter`)}
            />
          </section>
        );
      })}
    </div>
  );
}

/** `{}` filters nothing; the API stores it as no filter, so it is left out. */
function isEmptyObject(value: unknown): boolean {
  return typeof value === "object" && value !== null && !Array.isArray(value) && Object.keys(value).length === 0;
}

export const knowledgeStep: EditorStepDefinition = {
  id: "knowledge",
  label: "Knowledge",
  Component: KnowledgeStep,
  status: knowledgeStatus,
  ownsError: isKnowledgeError,
  specKeys: KNOWLEDGE_SPEC_KEYS,
  missing: (_draft, spec): MissingField[] => knowledgeProblems(spec).map((problem) => ({ step: "knowledge", fieldId: fieldId(problem.field), message: problem.message })),
};
