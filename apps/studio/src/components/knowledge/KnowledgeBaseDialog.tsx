"use client";

import React, { useId, useState } from "react";
import { ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/Collapsible";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { buildExtractConfig, createKnowledgeBase, EMPTY_EXTRACT_FORM, type ExtractForm, type KnowledgeBase, type KnowledgeBaseCreate } from "@/lib/knowledge";
import { EmbeddingModelPicker, pickedModelId, useEmbeddingModels } from "./EmbeddingModelPicker";
import { alertClass, fieldClass, hintClass, labelClass } from "./knowledgeUi";

interface KnowledgeBaseDialogProps {
  onCreated: (kb: KnowledgeBase) => void;
  onClose: () => void;
}

/**
 * The create request: `chunking.extract` only when an "Advanced extraction" field is set (else no
 * `chunking` at all, so the API's defaults apply: chunker v2 for new knowledge bases).
 */
export function knowledgeBaseCreateBody(fields: { name: string; description: string; embeddingModel: string; extract: ExtractForm }): { ok: true; body: KnowledgeBaseCreate } | { ok: false; reason: string } {
  const extract = buildExtractConfig(fields.extract);
  if (!extract.ok) return extract;
  const body: KnowledgeBaseCreate = { name: fields.name.trim(), description: fields.description.trim() || null, embedding_model: fields.embeddingModel };
  if (extract.value) body.chunking = { extract: extract.value };
  return { ok: true, body };
}

/**
 * "New knowledge base": name, description, the allowlisted embedding model, and (collapsed)
 * "Advanced extraction": Doxygen Markdown, rst config names and tags for `ifconfig`/`only`, and PDF
 * heading detection. Mounted while open.
 */
export function KnowledgeBaseDialog({ onCreated, onClose }: KnowledgeBaseDialogProps) {
  const id = useId();
  const { models, status, error: modelsError, reload } = useEmbeddingModels();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [choice, setChoice] = useState("");
  const [wildcardName, setWildcardName] = useState("");
  const [extract, setExtract] = useState<ExtractForm>({ ...EMPTY_EXTRACT_FORM });
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const modelId = pickedModelId(models, choice, wildcardName);
  const canSave = name.trim().length > 0 && modelId !== null && !saving;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSave || !modelId) return;
    setSaving(true);
    setError(null);
    try {
      const built = knowledgeBaseCreateBody({ name, description, embeddingModel: modelId, extract });
      if (!built.ok) {
        setAdvancedOpen(true);
        setError(built.reason);
        setSaving(false);
        return;
      }
      const kb = await createKnowledgeBase(built.body);
      onCreated(kb);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "The knowledge base could not be created");
      setSaving(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !saving) onClose(); }}>
      <DialogContent className="md:max-w-xl" data-testid="knowledge-create-dialog">
        <DialogHeader>
          <DialogTitle>New knowledge base</DialogTitle>
          <DialogDescription>Documents you upload are split into sections and embedded, so agents can retrieve and cite them.</DialogDescription>
        </DialogHeader>
        <form onSubmit={(event) => void submit(event)} className="min-w-0 space-y-4" aria-label="New knowledge base" data-testid="knowledge-create-form">
          {error ? <p role="alert" data-testid="knowledge-create-error" className={alertClass}>{error}</p> : null}
          <div className="space-y-1">
            <label htmlFor={`${id}-name`} className={labelClass}>Name</label>
            <input id={`${id}-name`} data-testid="knowledge-create-name" value={name} maxLength={100} required onChange={(event) => setName(event.target.value)} className={fieldClass} placeholder="Product docs" />
            <p className={hintClass}>Shown to the model in the retrieve tool; one line, up to 100 characters.</p>
          </div>
          <div className="space-y-1">
            <label htmlFor={`${id}-description`} className={labelClass}>Description <span className="font-normal text-muted-foreground">(optional)</span></label>
            <textarea id={`${id}-description`} data-testid="knowledge-create-description" value={description} maxLength={2000} rows={3} onChange={(event) => setDescription(event.target.value)} className={fieldClass} placeholder="What it covers, so the model knows when to search it." />
          </div>
          <EmbeddingModelPicker
            models={models}
            status={status}
            error={modelsError}
            onRetry={reload}
            choice={choice}
            onChoiceChange={setChoice}
            wildcardName={wildcardName}
            onWildcardNameChange={setWildcardName}
            disabled={saving}
          />
          <Collapsible open={advancedOpen} onOpenChange={setAdvancedOpen} className="min-w-0 rounded-md border border-border">
            <CollapsibleTrigger
              className="flex w-full min-w-0 items-center gap-2 rounded-md px-3 py-2 text-left text-label font-medium text-foreground outline-none hover:bg-fill-hover focus-visible:ring-2 focus-visible:ring-ring"
              data-testid="knowledge-create-advanced"
            >
              <ChevronRight strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className={cn("size-4 shrink-0 text-muted-foreground transition-transform", advancedOpen && "rotate-90")} />
              Advanced extraction
              <span className="font-normal text-muted-foreground">(optional)</span>
            </CollapsibleTrigger>
            <CollapsibleContent className="flex min-w-0 flex-col gap-4 border-t border-border p-3" data-testid="knowledge-create-advanced-fields">
              <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                <div className="min-w-0 space-y-1">
                  <label htmlFor={`${id}-md-flavor`} className={labelClass}>Markdown</label>
                  <select id={`${id}-md-flavor`} data-testid="knowledge-create-markdown-flavor" value={extract.markdownFlavor} disabled={saving} onChange={(event) => setExtract({ ...extract, markdownFlavor: event.target.value as ExtractForm["markdownFlavor"] })} className={fieldClass}>
                    <option value="">Plain</option>
                    <option value="doxygen">Doxygen</option>
                  </select>
                  <p className={hintClass}>Doxygen turns \page, \section, \note and \code into headings, notes and code.</p>
                </div>
                <div className="min-w-0 space-y-1">
                  <label htmlFor={`${id}-pdf-headings`} className={labelClass}>PDF headings</label>
                  <select id={`${id}-pdf-headings`} data-testid="knowledge-create-pdf-headings" value={extract.pdfHeadings} disabled={saving} onChange={(event) => setExtract({ ...extract, pdfHeadings: event.target.value as ExtractForm["pdfHeadings"] })} className={fieldClass}>
                    <option value="">Default (bookmarks, else font size)</option>
                    <option value="outline">Bookmarks</option>
                    <option value="font">Font size</option>
                    <option value="pages">One section per page</option>
                  </select>
                </div>
              </div>
              <div className="min-w-0 space-y-1">
                <label htmlFor={`${id}-rst-config`} className={labelClass}>reStructuredText config</label>
                <textarea id={`${id}-rst-config`} data-testid="knowledge-create-rst-config" rows={3} value={extract.rstConfig} disabled={saving} onChange={(event) => setExtract({ ...extract, rstConfig: event.target.value })} className={`${fieldClass} font-mono`} placeholder={"CONFIG_part_variant: AM62X\nCONFIG_family: AM62X"} />
                <p className={hintClass}>One <code>NAME: value</code> per line, for <code>ifconfig</code> conditions. Unknown conditions keep their text.</p>
              </div>
              <div className="min-w-0 space-y-1">
                <label htmlFor={`${id}-rst-tags`} className={labelClass}>reStructuredText tags</label>
                <input id={`${id}-rst-tags`} data-testid="knowledge-create-rst-tags" value={extract.rstTags} disabled={saving} onChange={(event) => setExtract({ ...extract, rstTags: event.target.value })} className={`${fieldClass} font-mono`} placeholder="am62x, linux" />
                <p className={hintClass}>Tags for <code>only</code> blocks, separated by commas or spaces.</p>
              </div>
            </CollapsibleContent>
          </Collapsible>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose} disabled={saving}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={!canSave} data-testid="knowledge-create-submit">
              {saving ? "Creating…" : "Create knowledge base"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
