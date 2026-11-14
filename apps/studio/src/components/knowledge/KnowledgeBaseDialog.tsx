"use client";

import React, { useId, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import { createKnowledgeBase, type KnowledgeBase } from "@/lib/knowledge";
import { EmbeddingModelPicker, pickedModelId, useEmbeddingModels } from "./EmbeddingModelPicker";
import { alertClass, fieldClass, hintClass, labelClass } from "./knowledgeUi";

interface KnowledgeBaseDialogProps {
  onCreated: (kb: KnowledgeBase) => void;
  onClose: () => void;
}

/** "New knowledge base": name, description and the allowlisted embedding model. Mounted while open. */
export function KnowledgeBaseDialog({ onCreated, onClose }: KnowledgeBaseDialogProps) {
  const id = useId();
  const { models, status, error: modelsError, reload } = useEmbeddingModels();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [choice, setChoice] = useState("");
  const [wildcardName, setWildcardName] = useState("");
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
      const kb = await createKnowledgeBase({ name: name.trim(), description: description.trim() || null, embedding_model: modelId });
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
