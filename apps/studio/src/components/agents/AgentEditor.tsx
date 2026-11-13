import React, { useState, useEffect, useCallback, useRef } from "react";
import { listTools, type ToolDefinition } from "@/lib/tools";
import type { AgentSpec, McpToolRef } from "@/lib/api";
import { mcpToolKey, withMcpTools } from "@/lib/mcpServers";
import { McpToolPicker } from "./McpToolPicker";
import { ProcessorsEditor } from "./ProcessorsEditor";
import { ContextFieldsEditor } from "./ContextFieldsEditor";
import {
  errorTarget,
  fetchModels,
  fetchProcessorCatalog,
  placeholderErrors,
  placeholders,
  sectionsDiffer,
  sectionsOf,
  withClientIds,
  withSpecSections,
  type ContextField,
  type EditableProcessor,
  type ModelOption,
  type ProcessorCatalog,
} from "./specSections";

function sameMcpTools(a: McpToolRef[], b: McpToolRef[] | undefined): boolean {
  const other = b ?? [];
  return a.length === other.length && a.every((ref) => other.some((item) => mcpToolKey(item) === mcpToolKey(ref)));
}

interface AgentEditorProps {
  initialData?: AgentSpec;
  onSubmit: (spec: AgentSpec) => Promise<void>;
  submitLabel: string;
  onDirtyChange?: (isDirty: boolean) => void;
  /**
   * The last save or publish error from the API, if any. Messages that name a processor
   * (`input_processors[0]...`), the instructions (`instructions: ...`) or a processor model are
   * shown next to that part of the form.
   */
  serverError?: string | null;
}

export function AgentEditor({ initialData, onSubmit, submitLabel, onDirtyChange, serverError }: AgentEditorProps) {
  const [name, setName] = useState(initialData?.name || "");
  const [instructions, setInstructions] = useState(initialData?.instructions || "");
  const [provider, setProvider] = useState(initialData?.model.provider || "");
  const [modelName, setModelName] = useState(initialData?.model.name || "");
  const [selectedTools, setSelectedTools] = useState<string[]>(initialData?.tools || []);
  const [mcpTools, setMcpTools] = useState<McpToolRef[]>(initialData?.mcp_tools || []);
  const [temperature, setTemperature] = useState<string>(initialData?.model_settings?.temperature?.toString() || "");
  const [maxTokens, setMaxTokens] = useState<string>(initialData?.model_settings?.max_tokens?.toString() || "");
  const [inputProcessors, setInputProcessors] = useState<EditableProcessor[]>(() => withClientIds(sectionsOf(initialData).input_processors));
  const [outputProcessors, setOutputProcessors] = useState<EditableProcessor[]>(() => withClientIds(sectionsOf(initialData).output_processors));
  // A server error naming `<phase>_processors[i]` is pinned to that processor's clientId when it
  // arrives, so it follows the processor through reordering and disappears when it is removed.
  const [pinnedServerError, setPinnedServerError] = useState<{ phase: "input" | "output"; clientId: string; message: string } | null>(null);
  const [contextFields, setContextFields] = useState<ContextField[]>(sectionsOf(initialData).context_fields);
  const [catalog, setCatalog] = useState<ProcessorCatalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [models, setModels] = useState<ModelOption[] | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const instructionsRef = useRef<HTMLTextAreaElement>(null);
  const [submitting, setSubmitting] = useState(false);
  const [isDirty, setIsDirty] = useState(false);
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [toolsError, setToolsError] = useState<string | null>(null);

  useEffect(() => {
    listTools()
      .then(setTools)
      .catch((error: unknown) => setToolsError(error instanceof Error ? error.message : "Failed to load tools"));
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchProcessorCatalog()
      .then((value) => { if (!cancelled) setCatalog(value); })
      .catch((error: unknown) => { if (!cancelled) setCatalogError(error instanceof Error ? error.message : "Failed to load processors"); });
    fetchModels()
      .then((value) => { if (!cancelled) setModels(value); })
      .catch((error: unknown) => { if (!cancelled) setModelsError(error instanceof Error ? error.message : "Failed to load models"); });
    return () => { cancelled = true; };
  }, []);

  const toggleTool = (tool: string) => {
    setSelectedTools((prev) =>
      prev.includes(tool) ? prev.filter((t) => t !== tool) : [...prev, tool]
    );
  };

  const computeDirty = useCallback(() => (
    name !== initialData?.name ||
    instructions !== initialData?.instructions ||
    provider !== initialData?.model.provider ||
    modelName !== initialData?.model.name ||
    selectedTools.length !== initialData?.tools.length ||
    selectedTools.some((t) => !initialData?.tools.includes(t)) ||
    !sameMcpTools(mcpTools, initialData?.mcp_tools) ||
    temperature !== (initialData?.model_settings?.temperature?.toString() || "") ||
    maxTokens !== (initialData?.model_settings?.max_tokens?.toString() || "") ||
    sectionsDiffer(
      { input_processors: inputProcessors, output_processors: outputProcessors, context_fields: contextFields },
      sectionsOf(initialData),
    )
  ), [name, instructions, provider, modelName, selectedTools, mcpTools, temperature, maxTokens, inputProcessors, outputProcessors, contextFields, initialData]);

  const updateDirtyState = useCallback(() => {
    const isDirty = computeDirty();
    setIsDirty(isDirty);
    onDirtyChange?.(isDirty);
  }, [computeDirty, onDirtyChange]);

  // Track initial data changes
  const prevDataRef = { current: initialData };
  if (initialData !== prevDataRef.current) {
    setName(initialData?.name || "");
    setInstructions(initialData?.instructions || "");
    setProvider(initialData?.model.provider || "");
    setModelName(initialData?.model.name || "");
    setSelectedTools(initialData?.tools || []);
    setMcpTools(initialData?.mcp_tools || []);
    setTemperature(initialData?.model_settings?.temperature?.toString() || "");
    setMaxTokens(initialData?.model_settings?.max_tokens?.toString() || "");
    setInputProcessors(withClientIds(sectionsOf(initialData).input_processors));
    setOutputProcessors(withClientIds(sectionsOf(initialData).output_processors));
    setContextFields(sectionsOf(initialData).context_fields);
    prevDataRef.current = initialData;
  }

  // Report the dirty state on mount and after every change
  useEffect(() => {
    updateDirtyState();
  }, [updateDirtyState]);

  const usedPlaceholders = placeholders(instructions);
  const instructionProblems = placeholderErrors(instructions, contextFields);
  const target = serverError ? errorTarget(serverError) : null;

  // Pin a processor error to the processor it named at the moment the error arrived. Only a new
  // serverError re-pins; edits to the lists afterwards must not move the message.
  useEffect(() => {
    const t = serverError ? errorTarget(serverError) : null;
    if (!serverError || t?.kind !== "processor") {
      setPinnedServerError(null);
      return;
    }
    const list = t.phase === "input" ? inputProcessors : outputProcessors;
    const processor = list[t.index];
    setPinnedServerError(processor ? { phase: t.phase, clientId: processor.clientId, message: serverError } : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverError]);

  const processorErrors = (phase: "input" | "output"): Record<string, string> =>
    pinnedServerError && pinnedServerError.phase === phase ? { [pinnedServerError.clientId]: pinnedServerError.message } : {};

  const insertPlaceholder = (fieldName: string) => {
    const token = `{{context.${fieldName}}}`;
    const element = instructionsRef.current;
    const start = element?.selectionStart ?? instructions.length;
    const end = element?.selectionEnd ?? instructions.length;
    setInstructions(instructions.slice(0, start) + token + instructions.slice(end));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;

    // mcp_tools, the processor lists and context_fields are left out entirely when empty, so the
    // spec hash stays stable for agents that do not use them.
    const spec: AgentSpec = withSpecSections(withMcpTools({
      spec_version: 1,
      name,
      instructions,
      model: {
        provider,
        name: modelName,
      },
      tools: selectedTools,
      model_settings: {
        temperature: temperature ? parseFloat(temperature) : null,
        max_tokens: maxTokens ? parseInt(maxTokens, 10) : null,
      },
    }, mcpTools), { input_processors: inputProcessors, output_processors: outputProcessors, context_fields: contextFields });

    setSubmitting(true);
    try {
      await onSubmit(spec);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      <div>
        <label htmlFor="name" className="block text-sm font-medium text-card-foreground mb-1">
          Name
        </label>
        <input
          id="name"
          type="text"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            updateDirtyState();
          }}
          className="w-full px-3 py-2 border border-border rounded-md focus:outline-none focus:ring-2 focus:ring-accent-foreground bg-card text-card-foreground"
          required
        />
      </div>

      <div>
        <label htmlFor="instructions" className="block text-sm font-medium text-card-foreground mb-1">
          Instructions
        </label>
        <textarea
          id="instructions"
          ref={instructionsRef}
          aria-invalid={instructionProblems.length > 0}
          aria-describedby="placeholder-hints instructions-problems"
          value={instructions}
          onChange={(e) => {
            setInstructions(e.target.value);
            updateDirtyState();
          }}
          rows={6}
          className="w-full px-3 py-2 border border-border rounded-md focus:outline-none focus:ring-2 focus:ring-accent-foreground bg-card text-card-foreground"
          required
        />
        <p id="placeholder-hints" data-testid="placeholder-hints" className="mt-1 text-xs text-muted-foreground">
          {contextFields.length > 0 ? (
            <>
              Context placeholders:{" "}
              {contextFields.map((field, i) => (
                <React.Fragment key={`${field.name}-${i}`}>
                  {i > 0 ? ", " : null}
                  <code className="font-mono">{`{{context.${field.name}}}`}</code>
                </React.Fragment>
              ))}
            </>
          ) : (
            <>Add request context fields below to use <code className="font-mono">{"{{context.<name>}}"}</code> placeholders.</>
          )}
        </p>
        {/* Polite and persistent: announced when the problems change, not on every keystroke. */}
        <div id="instructions-problems" aria-live="polite">
          {instructionProblems.map((problem) => (
            <p key={problem} data-testid="placeholder-error" className="mt-1 text-xs text-destructive">{problem}</p>
          ))}
        </div>
        {target?.kind === "instructions" && serverError && !instructionProblems.some((p) => serverError.endsWith(p)) ? (
          <p role="alert" data-testid="instructions-server-error" className="mt-1 text-xs text-destructive">{serverError}</p>
        ) : null}
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label htmlFor="provider" className="block text-sm font-medium text-card-foreground mb-1">
            Model provider
          </label>
          <input
            id="provider"
            type="text"
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value);
              updateDirtyState();
            }}
            className="w-full px-3 py-2 border border-border rounded-md focus:outline-none focus:ring-2 focus:ring-accent-foreground bg-card text-card-foreground"
            required
          />
        </div>
        <div>
          <label htmlFor="modelName" className="block text-sm font-medium text-card-foreground mb-1">
            Model name
          </label>
          <input
            id="modelName"
            type="text"
            value={modelName}
            onChange={(e) => {
              setModelName(e.target.value);
              updateDirtyState();
            }}
            className="w-full px-3 py-2 border border-border rounded-md focus:outline-none focus:ring-2 focus:ring-accent-foreground bg-card text-card-foreground"
            required
          />
        </div>
      </div>

      <div>
        <span className="block text-sm font-medium text-card-foreground mb-2">
          Tools
        </span>
        <div className="flex flex-wrap gap-2">
          {tools.map((tool) => (
            <label key={tool.id} className={`inline-flex items-center px-3 py-2 border border-border rounded-md bg-card ${tool.allowed ? "hover:bg-accent cursor-pointer" : "cursor-not-allowed opacity-60"}`}>
              <input
                type="checkbox"
                aria-label={`${tool.title} (${tool.id})`}
                className="form-checkbox h-4 w-4 text-accent-foreground focus:ring-accent-foreground border-border rounded"
                checked={selectedTools.includes(tool.id)}
                disabled={!tool.allowed}
                onChange={() => {
                  toggleTool(tool.id);
                  updateDirtyState();
                }}
              />
              <span className="ml-2 text-sm text-card-foreground">{tool.title} ({tool.id})</span>
              {!tool.allowed ? <span className="ml-2 text-xs text-muted-foreground">Disabled by platform policy</span> : null}
            </label>
          ))}
        </div>
        {toolsError ? <p role="alert" className="mt-2 text-xs text-destructive">{toolsError}</p> : null}
      </div>

      <McpToolPicker value={mcpTools} onChange={setMcpTools} />

      <ProcessorsEditor
        inputProcessors={inputProcessors}
        outputProcessors={outputProcessors}
        onInputChange={setInputProcessors}
        onOutputChange={setOutputProcessors}
        catalog={catalog}
        catalogError={catalogError}
        models={models}
        modelsError={modelsError}
        sectionError={target?.kind === "processors" ? serverError : null}
        inputErrors={processorErrors("input")}
        outputErrors={processorErrors("output")}
      />

      <ContextFieldsEditor
        value={contextFields}
        onChange={setContextFields}
        usedNames={usedPlaceholders}
        onInsertPlaceholder={insertPlaceholder}
        sectionError={target?.kind === "context_fields" ? serverError : null}
      />

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label htmlFor="temperature" className="block text-sm font-medium text-card-foreground mb-1">
            Temperature (0–2)
          </label>
          <input
            id="temperature"
            type="number"
            min={0}
            max={2}
            step={0.1}
            value={temperature}
            onChange={(e) => {
              setTemperature(e.target.value);
              updateDirtyState();
            }}
            className="w-full px-3 py-2 border border-border rounded-md focus:outline-none focus:ring-2 focus:ring-accent-foreground bg-card text-card-foreground"
          />
        </div>
        <div>
          <label htmlFor="maxTokens" className="block text-sm font-medium text-card-foreground mb-1">
            Max tokens (optional)
          </label>
          <input
            id="maxTokens"
            type="number"
            min={1}
            value={maxTokens}
            onChange={(e) => {
              setMaxTokens(e.target.value);
              updateDirtyState();
            }}
            className="w-full px-3 py-2 border border-border rounded-md focus:outline-none focus:ring-2 focus:ring-accent-foreground bg-card text-card-foreground"
          />
        </div>
      </div>

      <div className="pt-4">
        <button
          type="submit"
          disabled={submitting}
          className="w-full px-4 py-2 bg-accent text-accent-foreground rounded-md hover:bg-accent/90 focus:outline-none focus:ring-2 focus:ring-accent-foreground focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {submitting ? "Saving..." : submitLabel}
        </button>
      </div>
    </form>
  );
}
