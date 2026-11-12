import React, { useState, useEffect, useCallback } from "react";
import { listTools, type ToolDefinition } from "@/lib/tools";

interface AgentSpec {
  spec_version: 1;
  name: string;
  instructions: string;
  model: {
    provider: string;
    name: string;
  };
  tools: string[];
  model_settings?: {
    temperature?: number | null;
    max_tokens?: number | null;
  };
}

interface AgentEditorProps {
  initialData?: AgentSpec;
  onSubmit: (spec: AgentSpec) => Promise<void>;
  submitLabel: string;
  onDirtyChange?: (isDirty: boolean) => void;
}

export function AgentEditor({ initialData, onSubmit, submitLabel, onDirtyChange }: AgentEditorProps) {
  const [name, setName] = useState(initialData?.name || "");
  const [instructions, setInstructions] = useState(initialData?.instructions || "");
  const [provider, setProvider] = useState(initialData?.model.provider || "");
  const [modelName, setModelName] = useState(initialData?.model.name || "");
  const [selectedTools, setSelectedTools] = useState<string[]>(initialData?.tools || []);
  const [temperature, setTemperature] = useState<string>(initialData?.model_settings?.temperature?.toString() || "");
  const [maxTokens, setMaxTokens] = useState<string>(initialData?.model_settings?.max_tokens?.toString() || "");
  const [submitting, setSubmitting] = useState(false);
  const [isDirty, setIsDirty] = useState(false);
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [toolsError, setToolsError] = useState<string | null>(null);

  useEffect(() => {
    listTools()
      .then(setTools)
      .catch((error: unknown) => setToolsError(error instanceof Error ? error.message : "Failed to load tools"));
  }, []);

  const toggleTool = (tool: string) => {
    setSelectedTools((prev) =>
      prev.includes(tool) ? prev.filter((t) => t !== tool) : [...prev, tool]
    );
  };

  const updateDirtyState = useCallback(() => {
    const isDirty = 
      name !== initialData?.name ||
      instructions !== initialData?.instructions ||
      provider !== initialData?.model.provider ||
      modelName !== initialData?.model.name ||
      selectedTools.length !== initialData?.tools.length ||
      selectedTools.some((t) => !initialData?.tools.includes(t)) ||
      temperature !== (initialData?.model_settings?.temperature?.toString() || "") ||
      maxTokens !== (initialData?.model_settings?.max_tokens?.toString() || "");
    setIsDirty(isDirty);
    onDirtyChange?.(isDirty);
  }, [name, instructions, provider, modelName, selectedTools, temperature, maxTokens, initialData, onDirtyChange]);

  // Track initial data changes
  const prevDataRef = { current: initialData };
  if (initialData !== prevDataRef.current) {
    setName(initialData?.name || "");
    setInstructions(initialData?.instructions || "");
    setProvider(initialData?.model.provider || "");
    setModelName(initialData?.model.name || "");
    setSelectedTools(initialData?.tools || []);
    setTemperature(initialData?.model_settings?.temperature?.toString() || "");
    setMaxTokens(initialData?.model_settings?.max_tokens?.toString() || "");
    prevDataRef.current = initialData;
  }

  // Report initial dirty state on mount
  useEffect(() => {
    const isDirty = 
      name !== initialData?.name ||
      instructions !== initialData?.instructions ||
      provider !== initialData?.model.provider ||
      modelName !== initialData?.model.name ||
      selectedTools.length !== initialData?.tools.length ||
      selectedTools.some((t) => !initialData?.tools.includes(t)) ||
      temperature !== (initialData?.model_settings?.temperature?.toString() || "") ||
      maxTokens !== (initialData?.model_settings?.max_tokens?.toString() || "");
    setIsDirty(isDirty);
    onDirtyChange?.(isDirty);
  }, [name, instructions, provider, modelName, selectedTools, temperature, maxTokens, initialData, onDirtyChange]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;

    const spec: AgentSpec = {
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
    };

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
          value={instructions}
          onChange={(e) => {
            setInstructions(e.target.value);
            updateDirtyState();
          }}
          rows={6}
          className="w-full px-3 py-2 border border-border rounded-md focus:outline-none focus:ring-2 focus:ring-accent-foreground bg-card text-card-foreground"
          required
        />
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
