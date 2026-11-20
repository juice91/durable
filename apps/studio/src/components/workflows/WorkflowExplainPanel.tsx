"use client";

import React from "react";
import { Loader2, X } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { formatDateTime } from "@/components/approvals/ApprovalCard";
import type { ExplanationControl } from "@/lib/workflowExplanations";

function Section({ title, children, testId }: { title: string; children: React.ReactNode; testId: string }) {
  return (
    <section data-testid={testId} aria-label={title} className="min-w-0 space-y-1.5">
      <h3 className="text-label font-semibold text-foreground">{title}</h3>
      {children}
    </section>
  );
}

/**
 * 「AI 說明」: the right-side panel of the Graph tab. It shows the cached explanation of the selected published
 * version (summary, inputs, endings, steps) or offers to generate it. Everything is plain selectable text.
 * `published` false: a draft has no explanation.
 */
export function WorkflowExplainPanel({ control, published, onClose }: { control: ExplanationControl; published: boolean; onClose: () => void }) {
  const { status, explanation, error, generate } = control;
  const retry = (
    <Button size="sm" variant="primary" data-testid="workflow-explain-generate" onClick={() => generate(false)}>
      產生說明
    </Button>
  );
  let body: React.ReactNode;
  if (!published) {
    body = <p data-testid="workflow-explain-draft" className="text-label text-muted-foreground">發佈後可產生說明</p>;
  } else if (status === "generating") {
    body = (
      <p role="status" data-testid="workflow-explain-loading" className="flex items-center gap-2 text-label text-muted-foreground">
        <Loader2 aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-4 animate-spin" />
        正在產生…
      </p>
    );
  } else if (status === "loading") {
    body = <p role="status" className="text-label text-muted-foreground">載入中…</p>;
  } else if (status === "error") {
    body = (
      <div className="space-y-2">
        <p role="alert" data-testid="workflow-explain-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-label text-destructive-text">{error}</p>
        <Button size="sm" data-testid="workflow-explain-retry" onClick={() => generate(explanation !== null)}>重試</Button>
      </div>
    );
  } else if (status === "ready" && explanation) {
    body = (
      <div data-testid="workflow-explain-content" className="min-w-0 select-text space-y-4">
        <p data-testid="workflow-explain-meta" className="text-caption text-muted-foreground">
          {explanation.model ? <span className="font-mono">{explanation.model}</span> : null}
          {explanation.model ? " · " : ""}{formatDateTime(explanation.created_at)}
          {" · "}
          <button type="button" data-testid="workflow-explain-refresh" onClick={() => generate(true)} className="rounded-sm text-info-text underline-offset-4 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring">重新產生</button>
        </p>
        <p data-testid="workflow-explain-summary" className="whitespace-pre-wrap break-words text-body text-foreground">{explanation.summary}</p>
        {explanation.inputs.length > 0 ? (
          <Section title="輸入" testId="workflow-explain-inputs">
            <ul className="space-y-1.5">
              {explanation.inputs.map((item) => (
                <li key={item.name} data-testid="workflow-explain-input" className="min-w-0 text-label">
                  <code className="break-all font-mono text-foreground">{item.name}</code>
                  <span className="block break-words text-muted-foreground">{item.meaning}</span>
                </li>
              ))}
            </ul>
          </Section>
        ) : null}
        {explanation.outcomes.length > 0 ? (
          <Section title="結束狀態" testId="workflow-explain-outcomes">
            <ul className="space-y-1.5">
              {explanation.outcomes.map((item, index) => (
                <li key={`${item.status}-${index}`} data-testid="workflow-explain-outcome" className="min-w-0 text-label">
                  <code className="break-all font-mono text-foreground">{item.status}</code>
                  <span className="block break-words text-muted-foreground">{item.meaning}</span>
                </li>
              ))}
            </ul>
          </Section>
        ) : null}
        {explanation.steps.length > 0 ? (
          <Section title="步驟" testId="workflow-explain-steps">
            <ul className="space-y-1.5">
              {explanation.steps.map((item) => (
                <li key={item.id} data-testid="workflow-explain-step" className="min-w-0 text-label">
                  <code className="break-all font-mono text-foreground">{item.id}</code>
                  <span className="block break-words text-muted-foreground">{item.note}</span>
                </li>
              ))}
            </ul>
          </Section>
        ) : null}
      </div>
    );
  } else {
    body = (
      <div className="space-y-2">
        <p className="text-label text-muted-foreground">用白話說明這個工作流與每個步驟在做什麼。說明只描述已發佈的版本，不會更動工作流。</p>
        {retry}
      </div>
    );
  }
  return (
    <aside
      aria-label="AI 說明"
      data-testid="workflow-explain-panel"
      className="absolute bottom-4 right-4 top-4 z-10 flex w-[22rem] max-w-[calc(100%-2rem)] min-w-0 flex-col rounded-lg border border-border bg-surface-raised shadow-overlay"
    >
      <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
        <h2 className="text-label font-semibold text-foreground">AI 說明</h2>
        <button type="button" data-testid="workflow-explain-close" onClick={onClose} aria-label="關閉 AI 說明" className="rounded-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
          <X aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-4" />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">{body}</div>
    </aside>
  );
}
