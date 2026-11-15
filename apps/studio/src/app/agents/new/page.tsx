"use client";
import { readableError } from "@/components/agents/readableError";
import { Suspense, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { AgentSpec } from "@/lib/api";
import { AgentEditor } from "@/components/agents/AgentEditor";
import { useEditorLayout } from "@/components/agents/stepper/useEditorLayout";
import { stepForError } from "@/components/agents/stepper/registry";
import { useEditorQuery } from "@/components/agents/stepper/useEditorQuery";
import { useUnsavedChangesGuard } from "@/components/agents/stepper/useUnsavedChangesGuard";
import { routes } from "@/lib/routes";
import { PageHeader } from "@/components/frame/PageHeader";
import { navCrumb } from "@/lib/nav";

/** The create form: the same stepper as the Editor tab, with "Create agent" in place of Save and Publish. */
function NewAgentForm() {
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [created, setCreated] = useState(false);
  const router = useRouter();
  const query = useEditorQuery();
  const layout = useEditorLayout();
  const guardDialog = useUnsavedChangesGuard(dirty && !created);

  const handleSubmit = async (spec: AgentSpec) => {
    setError(null);
    try {
      const agent = await api.agents.create(spec);
      setCreated(true);
      router.push(routes.agentEditor(agent.id));
    } catch (err: unknown) {
      setError(err instanceof Error && err.message ? err.message : "Failed to create agent");
    }
  };

  return (
    <div data-testid="agent-create" className="w-full min-w-0">
      <AgentEditor
        onSubmit={handleSubmit}
        submitLabel="Create agent"
        onDirtyChange={setDirty}
        serverError={error}
        stepper={{
          activeStep: query.step,
          onStepChange: query.setStep,
          layout,
          submitVariant: "primary",
          notices: error && !stepForError(error) ? (
            <div role="alert" data-testid="agent-create-error" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-label text-destructive-text">
              {readableError(error)}
            </div>
          ) : undefined,
        }}
      />
      {guardDialog}
    </div>
  );
}

export default function NewAgentPage() {
  return (
    <>
      <PageHeader crumbs={[navCrumb("agents"), { label: "New Agent" }]} />
      <Suspense fallback={null}>
        <NewAgentForm />
      </Suspense>
    </>
  );
}
