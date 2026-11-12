"use client";

import React, { useEffect, useMemo, useState } from "react";
import { JsonSchemaForm } from "@/components/tools/JsonSchemaForm";
import { listTools, type ToolDefinition } from "@/lib/tools";

type InputMode = "form" | "json";

export default function ToolsPage() {
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<InputMode>("form");
  const [input, setInput] = useState<Record<string, unknown>>({});
  const [rawInput, setRawInput] = useState("{}");
  const [jsonError, setJsonError] = useState<string | null>(null);

  useEffect(() => {
    listTools().then((catalog) => {
      setTools(catalog);
      setSelectedId(catalog[0]?.id ?? null);
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Failed to load tools"))
      .finally(() => setLoading(false));
  }, []);

  const selected = tools.find((tool) => tool.id === selectedId) || null;
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return tools.filter((tool) => !needle || `${tool.title} ${tool.id} ${tool.description}`.toLowerCase().includes(needle));
  }, [query, tools]);

  const selectTool = (id: string) => {
    setSelectedId(id);
    setInput({});
    setRawInput("{}");
    setJsonError(null);
    setMode("form");
  };

  const setFormInput = (next: Record<string, unknown>) => {
    setInput(next);
    setRawInput(JSON.stringify(next, null, 2));
  };

  const setJsonInput = (raw: string) => {
    setRawInput(raw);
    try {
      const parsed: unknown = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
      setInput(parsed as Record<string, unknown>);
      setJsonError(null);
    } catch {
      setJsonError("Enter valid JSON");
    }
  };

  return (
    <div className="mx-auto max-w-7xl px-4 py-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-foreground">Tools</h1>
        <p className="mt-1 text-muted-foreground">Explore platform tools and prepare their inputs.</p>
      </div>
      {error ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</p> : null}
      {loading ? <p className="text-muted-foreground">Loading tools…</p> : null}
      {!loading && !error ? (
        <div className="grid gap-6 lg:grid-cols-[20rem_minmax(0,1fr)]">
          <aside className="space-y-3">
            <label htmlFor="tool-search" className="sr-only">Search tools</label>
            <input id="tool-search" type="search" placeholder="Search tools" value={query} onChange={(event) => setQuery(event.target.value)} className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
            <div className="space-y-2" aria-label="Tool catalog">
              {filtered.map((tool) => (
                <button key={tool.id} type="button" onClick={() => selectTool(tool.id)} className={`w-full rounded-md border p-3 text-left transition-colors ${selectedId === tool.id ? "border-primary bg-accent" : "border-border bg-card hover:bg-accent/60"}`}>
                  <span className="block font-medium text-card-foreground">{tool.title}</span>
                  <span className="block font-mono text-xs text-muted-foreground">{tool.id}</span>
                  <span className="mt-2 line-clamp-2 block text-sm text-muted-foreground">{tool.description}</span>
                  <span className="mt-2 flex flex-wrap gap-1">
                    {tool.side_effect ? <span className="rounded-full bg-destructive/10 px-2 py-0.5 text-xs text-destructive">Side effect</span> : <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">Read only</span>}
                    <span className={`rounded-full px-2 py-0.5 text-xs ${tool.allowed ? "bg-primary/10 text-primary" : "bg-destructive/10 text-destructive"}`}>{tool.allowed ? "Allowed" : "Disallowed"}</span>
                  </span>
                </button>
              ))}
              {filtered.length === 0 ? <p className="py-6 text-center text-sm text-muted-foreground">No tools match your search.</p> : null}
            </div>
          </aside>
          {selected ? (
            <main className="min-w-0 space-y-6">
              <section className="rounded-lg border border-border bg-card p-5">
                <h2 className="text-xl font-semibold text-card-foreground">{selected.title}</h2>
                <p className="font-mono text-xs text-muted-foreground">{selected.id}</p>
                <p className="mt-3 text-sm text-muted-foreground">{selected.description}</p>
              </section>
              <section className="rounded-lg border border-border bg-card p-5">
                <div className="mb-4 flex items-center justify-between gap-4">
                  <h3 className="font-semibold text-card-foreground">Input</h3>
                  <div className="flex rounded-md border border-border p-1">
                    {(["form", "json"] as const).map((item) => <button key={item} type="button" onClick={() => setMode(item)} className={`rounded px-3 py-1 text-sm ${mode === item ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent"}`}>{item === "form" ? "Form" : "JSON"}</button>)}
                  </div>
                </div>
                {mode === "form" ? <JsonSchemaForm schema={selected.input_schema} value={input} onChange={setFormInput} /> : (
                  <div className="space-y-1">
                    <label htmlFor="tool-input-json" className="sr-only">Tool input JSON</label>
                    <textarea id="tool-input-json" rows={12} value={rawInput} onChange={(event) => setJsonInput(event.target.value)} className="w-full rounded-md border border-input bg-background p-3 font-mono text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
                    {jsonError ? <p role="alert" className="text-xs text-destructive">{jsonError}</p> : null}
                  </div>
                )}
                <div className="mt-5 border-t border-border pt-4">
                  <button type="button" disabled className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50">Run</button>
                  <p className="mt-2 text-xs text-muted-foreground">Running tools arrives with tool test runs</p>
                </div>
              </section>
              <section className="rounded-lg border border-border bg-card p-5">
                <h3 className="mb-3 font-semibold text-card-foreground">Input schema</h3>
                <pre className="overflow-x-auto rounded-md bg-muted p-4 text-xs text-foreground">{JSON.stringify(selected.input_schema, null, 2)}</pre>
              </section>
            </main>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
