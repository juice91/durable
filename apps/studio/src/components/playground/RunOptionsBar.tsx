"use client";

import React from "react";
import type { Agent, AgentVersion } from "@/lib/api";
import type { ModelOption } from "@/lib/metrics";
import {
  isWildcardChoice,
  modelPickerOptions,
  versionFromSelectValue,
  versionSelectValue,
  type ModelChoice,
  type VersionRef,
} from "./runOptions";

export interface RunOptionsBarProps {
  agents: Agent[];
  selectedAgentId: string;
  onAgentChange: (agentId: string) => void;

  /** The selected agent's published versions, newest first; null while loading. */
  versions: AgentVersion[] | null;
  latestVersion: number | null;
  /** The effective target (an unpublished agent is always "draft"). */
  versionRef: VersionRef;
  onVersionChange: (ref: VersionRef) => void;
  versionError: string | null;

  /** The target spec's own model, `provider:name`; null while unknown. */
  defaultModelId: string | null;
  models: ModelOption[];
  modelsError: string | null;
  modelChoice: ModelChoice;
  onModelChoiceChange: (choice: ModelChoice) => void;
  modelError: string | null;
  modelNotice: string | null;

  contextOpen: boolean;
  onToggleContext: () => void;
  /** Declared context fields of the target; null while unknown. */
  contextFieldCount: number | null;
  /** Number of context values that will be sent with the next turn. */
  contextValueCount: number;
  contextHasErrors: boolean;

  disabled?: boolean;
}

const selectClass =
  "rounded-md border border-border bg-card px-3 py-2 text-sm text-card-foreground disabled:cursor-not-allowed disabled:opacity-60";

function FieldError({ id, message }: { id: string; message: string | null }) {
  if (!message) return null;
  return (
    <p id={id} role="alert" className="mt-1 max-w-sm text-xs text-destructive">
      {message}
    </p>
  );
}

/** Agent, version (or draft), model and request-context controls of the playground. */
export function RunOptionsBar(props: RunOptionsBarProps) {
  const {
    agents,
    selectedAgentId,
    onAgentChange,
    versions,
    latestVersion,
    versionRef,
    onVersionChange,
    versionError,
    defaultModelId,
    models,
    modelsError,
    modelChoice,
    onModelChoiceChange,
    modelError,
    modelNotice,
    contextOpen,
    onToggleContext,
    contextFieldCount,
    contextValueCount,
    contextHasErrors,
    disabled = false,
  } = props;

  const pickerOptions = modelPickerOptions(models, defaultModelId);
  const wildcard = isWildcardChoice(modelChoice.choice);
  // A choice the list no longer offers (e.g. models still loading) stays visible as itself.
  const choiceListed = modelChoice.choice === "" || pickerOptions.some((o) => o.value === modelChoice.choice);
  const listed = [...(versions ?? [])].sort((a, b) => b.version - a.version);

  return (
    <div className="flex flex-wrap items-start gap-4" data-testid="run-options">
      <label className="flex flex-col gap-1 text-sm font-medium text-muted-foreground">
        Agent
        <select
          aria-label="Agent"
          className={selectClass}
          value={selectedAgentId}
          onChange={(event) => onAgentChange(event.target.value)}
        >
          {agents.length === 0 && <option value="">No agents</option>}
          {agents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.name}
              {agent.latest_version === null && " (draft only)"}
            </option>
          ))}
        </select>
      </label>

      <div className="flex flex-col gap-1">
        <label htmlFor="playground-version" className="text-sm font-medium text-muted-foreground">
          Version
        </label>
        <select
          id="playground-version"
          aria-label="Version"
          aria-invalid={Boolean(versionError)}
          aria-describedby={versionError ? "playground-version-error" : undefined}
          className={selectClass}
          value={versionSelectValue(versionRef)}
          disabled={disabled || !selectedAgentId}
          onChange={(event) => onVersionChange(versionFromSelectValue(event.target.value))}
        >
          {latestVersion !== null && <option value="latest">Latest (v{latestVersion})</option>}
          {listed.map((v) => (
            <option key={v.version} value={`v${v.version}`}>
              v{v.version}
              {v.restored_from ? ` (restored from v${v.restored_from})` : ""}
            </option>
          ))}
          {typeof versionRef === "number" && !listed.some((v) => v.version === versionRef) && (
            <option value={`v${versionRef}`}>v{versionRef}</option>
          )}
          <option value="draft">Draft (unpublished)</option>
        </select>
        <FieldError id="playground-version-error" message={versionError} />
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="playground-model" className="text-sm font-medium text-muted-foreground">
          Model
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <select
            id="playground-model"
            aria-label="Model"
            aria-invalid={Boolean(modelError)}
            aria-describedby={modelError ? "playground-model-error" : undefined}
            className={selectClass}
            value={modelChoice.choice}
            disabled={disabled || !selectedAgentId}
            onChange={(event) => onModelChoiceChange({ choice: event.target.value, customName: "" })}
          >
            <option value="">
              {defaultModelId ? `Version default (${defaultModelId})` : "Version default"}
            </option>
            {pickerOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
            {!choiceListed && <option value={modelChoice.choice}>{modelChoice.choice}</option>}
          </select>
          {wildcard && (
            <input
              aria-label="Model name"
              placeholder="model name"
              className="w-44 rounded-md border border-border bg-card px-3 py-2 text-sm text-card-foreground"
              value={modelChoice.customName}
              disabled={disabled}
              onChange={(event) => onModelChoiceChange({ choice: modelChoice.choice, customName: event.target.value })}
            />
          )}
        </div>
        <FieldError id="playground-model-error" message={modelError} />
        {!modelError && modelNotice && <p className="mt-1 max-w-sm text-xs text-muted-foreground">{modelNotice}</p>}
        {!modelError && modelsError && (
          <p className="mt-1 max-w-sm text-xs text-muted-foreground">{modelsError}</p>
        )}
      </div>

      <div className="flex flex-col gap-1">
        <span id="playground-context-label" className="text-sm font-medium text-muted-foreground">
          Request context
        </span>
        <button
          id="playground-context-toggle"
          type="button"
          aria-labelledby="playground-context-label playground-context-toggle"
          aria-expanded={contextOpen}
          aria-controls="playground-context-panel"
          onClick={onToggleContext}
          disabled={!selectedAgentId}
          className={`rounded-md border px-3 py-2 text-left text-sm disabled:cursor-not-allowed disabled:opacity-60 ${
            contextHasErrors
              ? "border-destructive/50 bg-destructive/10 text-destructive"
              : "border-border bg-card text-card-foreground hover:bg-accent"
          }`}
        >
          {contextFieldCount === null
            ? "Context"
            : contextFieldCount === 0
              ? "No context fields"
              : `${contextValueCount} of ${contextFieldCount} set`}
          {contextHasErrors ? " (problems)" : ""}
          <span aria-hidden="true"> {contextOpen ? "▴" : "▾"}</span>
        </button>
      </div>
    </div>
  );
}
