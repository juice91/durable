"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { PageHeader } from "@/components/frame/PageHeader";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/Tabs";
import { Skeleton } from "@/components/ui/Skeleton";
import { api } from "@/lib/api";
import {
  ArchitectError,
  getArchitect,
  getArchitectOptions,
  setArchitectModel,
  seedArchitect,
  storedSpecToArchitect,
  type ArchitectOptions,
  type ArchitectState,
  type CreatedItem,
} from "@/lib/architect";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";
import { ArchitectChat } from "./ArchitectChat";
import { ArchitectModelProvider, ModelPicker } from "./ArchitectModel";
import { ArchitectForm, type ArchitectFormSource } from "./ArchitectForm";
import { CreatedList } from "./CreatedList";
import { WorkflowTab } from "./WorkflowTab";

type Load = { kind: "loading" } | { kind: "choose_model" } | { kind: "error"; message: string } | { kind: "ready"; state: ArchitectState };

/**
 * The Architect page: left, "New" (agent or workflow) and what was created (`id · vN`); right, tabs Form | Chat |
 * Workflow. The Chat tab (`ArchitectChat`) and the Workflow tab (`WorkflowTab`) are mounted here;
 * both call `refresh` when they create or change something.
 */
export function ArchitectPage() {
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [options, setOptions] = useState<ArchitectOptions | null>(null);
  const [tab, setTab] = useState("form");
  const [source, setSource] = useState<{ key: number; value: ArchitectFormSource }>({ key: 0, value: { spec: null, version: null, agentRowId: null } });
  const [selected, setSelected] = useState<CreatedItem | null>(null);
  const [openError, setOpenError] = useState<string | null>(null);
  const seq = useRef(0);

  const refresh = useCallback(async () => {
    const ticket = ++seq.current;
    try {
      let state = await getArchitect();
      if (ticket !== seq.current) return;
      if (!state.seeded) {
        if (state.choose_model) {
          setLoad({ kind: "choose_model" });
          return;
        }
        try {
          await seedArchitect();
        } catch (reason: unknown) {
          if (reason instanceof ArchitectError && reason.code === "choose_model") {
            if (ticket === seq.current) setLoad({ kind: "choose_model" });
            return;
          }
          throw reason;
        }
        state = await getArchitect();
      }
      if (ticket === seq.current) setLoad({ kind: "ready", state });
    } catch (reason: unknown) {
      if (ticket === seq.current) setLoad({ kind: "error", message: reason instanceof Error ? reason.message : "Failed to load the Architect" });
    }
  }, []);

  useEffect(() => {
    void refresh();
    getArchitectOptions().then(setOptions, () => setOptions(null));
    return () => {
      seq.current += 1;
    };
  }, [refresh]);

  const [workflowKey, setWorkflowKey] = useState(0);
  const startNew = useCallback((kind: "agent" | "workflow") => {
    setSelected(null);
    setOpenError(null);
    if (kind === "workflow") {
      setWorkflowKey((current) => current + 1);  // a fresh workflow form
      setTab("workflow");
      return;
    }
    setSource((current) => ({ key: current.key + 1, value: { spec: null, version: null, agentRowId: null } }));
    setTab("form");
  }, []);

  const openAgent = useCallback(async (item: CreatedItem) => {
    if (!item.agent_id) return;
    setSelected(item);
    setOpenError(null);
    setTab("form");
    try {
      const agent = await api.agents.get(item.agent_id);
      setSource((current) => ({
        key: current.key + 1,
        value: { spec: storedSpecToArchitect(agent.draft_spec as unknown as Record<string, unknown>, item.id), version: agent.latest_version, agentRowId: item.agent_id ?? null },
      }));
    } catch (reason: unknown) {
      setOpenError(reason instanceof Error ? reason.message : "Failed to open the agent");
    }
  }, []);

  const state = load.kind === "ready" ? load.state : null;
  const changeModel = useCallback(async (model: string) => {
    await setArchitectModel(model);
    await refresh();
  }, [refresh]);
  const modelValue = useMemo(() => ({ model: state?.model ?? null, options, change: changeModel }), [state?.model, options, changeModel]);

  return (
    <>
      <PageHeader crumbs={[navCrumb("architect", { current: true })]} />
      <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-4 px-4 py-6">
        <p className="text-body text-muted-foreground">Describe an agent in plain words, review the form the Architect fills in, then save it as a draft or go live.</p>
        {load.kind === "loading" ? (
          <div className="space-y-3" aria-busy="true"><Skeleton className="h-8 w-64" /><Skeleton className="h-48 w-full" /></div>
        ) : load.kind === "choose_model" ? (
          <div data-testid="architect-choose-model" className="space-y-2 rounded-md border border-warning/40 bg-warning/10 p-4 text-body">
            <p className="font-medium">Choose a model for the Architect first.</p>
            <p className="text-label text-muted-foreground">No model is available yet, so the Architect cannot be set up. Connect a model provider, then come back.</p>
            <Link className="text-label underline underline-offset-4" href={routes.settingsProviders()}>Open providers</Link>
          </div>
        ) : load.kind === "error" ? (
          <div role="alert" className="space-y-2 rounded-md border border-destructive/40 bg-destructive/10 p-4 text-label text-destructive-text">
            <p>{load.message}</p>
            <button type="button" className="underline underline-offset-4" onClick={() => void refresh()}>Try again</button>
          </div>
        ) : state ? (
          <ArchitectModelProvider value={modelValue}>
          <div data-testid="architect-model" className="flex flex-wrap items-center gap-2 text-label text-muted-foreground">
            <span>Model: <span className="font-mono text-foreground">{state.model ?? "none"}</span></span>
            <ModelPicker label="Change model" />
          </div>
          <div className="grid min-w-0 gap-6 md:grid-cols-[16rem_minmax(0,1fr)]">
            <aside aria-label="Created by the Architect">
              <CreatedList agents={state.agents} workflows={state.workflows} selectedAgentId={selected?.id ?? null} onNew={startNew} onSelectAgent={(item) => void openAgent(item)} />
            </aside>
            <div className="min-w-0">
              <Tabs value={tab} onValueChange={setTab}>
                <TabsList aria-label="Architect sections">
                  <TabsTrigger value="form">Form</TabsTrigger>
                  <TabsTrigger value="chat">Chat</TabsTrigger>
                  <TabsTrigger value="workflow">Workflow</TabsTrigger>
                </TabsList>
                <TabsContent value="form" className="pt-4">
                  {openError ? <div role="alert" className="mb-3 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-label text-destructive-text">{openError}</div> : null}
                  <ArchitectForm
                    key={source.key}
                    options={options}
                    source={source.value}
                    onSaved={() => void refresh()}
                    onReload={() => selected && void openAgent(selected)}
                  />
                </TabsContent>
                <TabsContent value="chat" className="pt-4">
                  <ArchitectChat architectAgentId={state.architect_agent_id} onChanged={() => void refresh()} />
                </TabsContent>
                <TabsContent value="workflow" className="pt-4">
                  <WorkflowTab key={workflowKey} options={options} agents={state.agents} onSaved={() => void refresh()} />
                </TabsContent>
              </Tabs>
            </div>
          </div>
          </ArchitectModelProvider>
        ) : null}
      </div>
    </>
  );
}
