"use client";

import React, { useEffect, useId, useRef, useState } from "react";
import { Upload } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Badge, type BadgeProps } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetFooter, SheetHeader } from "@/components/ui/Sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/Tabs";
import {
  checkUploadFile,
  documentKeyFrom,
  documentKeyProblem,
  documentPutProblem,
  parseMetadata,
  putDocument,
  readFileAsText,
  UPLOAD_ACCEPT,
  type DocumentPutResult,
  type MetadataValue,
} from "@/lib/knowledge";
import { alertClass, fieldClass, hintClass, labelClass } from "./knowledgeUi";

type ItemStatus = "refused" | "ready" | "uploading" | "started" | "unchanged" | "failed";

interface UploadItem {
  id: number;
  name: string;
  file: File | null;
  key: string | null;
  title: string;
  mimeType: string | null;
  status: ItemStatus;
  message: string | null;
}

const STATUS_LABEL: Record<ItemStatus, string> = {
  refused: "Not supported",
  ready: "Ready",
  uploading: "Uploading…",
  started: "Ingest started",
  unchanged: "Unchanged",
  failed: "Failed",
};

const STATUS_VARIANT: Record<ItemStatus, BadgeProps["variant"]> = {
  refused: "destructive",
  ready: "neutral",
  uploading: "info",
  started: "success",
  unchanged: "neutral",
  failed: "destructive",
};

const PASTE_FORMATS = [
  { value: "text/markdown", label: "Markdown" },
  { value: "text/plain", label: "Plain text" },
  { value: "text/html", label: "HTML" },
] as const;

let itemCounter = 0;

export interface UploadSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kbId: string;
  kbName: string;
  /** A document was stored (a new version and its ingest run, or unchanged). */
  onUploaded: (result: DocumentPutResult) => void;
}

/**
 * Upload documents: a drop zone (and file picker) for several .md, .txt or .html files at once, or
 * pasted text. Files are read as UTF-8 in the browser and sent one by one (PUT …/documents/{key});
 * the document key is the file name. Unsupported files are refused here with the reason.
 */
export function UploadSheet({ open, onOpenChange, kbId, kbName, onUploaded }: UploadSheetProps) {
  const id = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [items, setItems] = useState<UploadItem[]>([]);
  const [metadataText, setMetadataText] = useState("");
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Paste
  const [pasteKey, setPasteKey] = useState("");
  const [pasteTitle, setPasteTitle] = useState("");
  const [pasteFormat, setPasteFormat] = useState<string>("text/markdown");
  const [pasteUrl, setPasteUrl] = useState("");
  const [pasteContent, setPasteContent] = useState("");
  const [pasteMetadata, setPasteMetadata] = useState("");
  const [pasteResult, setPasteResult] = useState<{ ok: boolean; message: string } | null>(null);
  // Aborted on unmount: no request outlives the Sheet's page, and no state is set after it.
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);
  const startBatch = () => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    return controller.signal;
  };
  const isAbort = (reason: unknown) => reason instanceof DOMException && reason.name === "AbortError";

  const addFiles = (files: FileList | File[] | null) => {
    if (!files) return;
    const added: UploadItem[] = Array.from(files).map((file) => {
      const check = checkUploadFile(file);
      itemCounter += 1;
      return check.ok
        ? { id: itemCounter, name: file.name, file, key: check.key, title: check.title, mimeType: check.mimeType, status: "ready", message: null }
        : { id: itemCounter, name: file.name, file: null, key: null, title: file.name, mimeType: null, status: "refused", message: check.reason };
    });
    setItems((current) => [...current, ...added]);
  };

  const update = (itemId: number, changes: Partial<UploadItem>) =>
    setItems((current) => current.map((item) => (item.id === itemId ? { ...item, ...changes } : item)));

  /** Refuses a body over the cap before sending (the API would answer 413). */
  const send = async (key: string, body: Parameters<typeof putDocument>[2], signal: AbortSignal): Promise<DocumentPutResult> => {
    const tooBig = documentPutProblem(key, body);
    if (tooBig) throw new Error(tooBig);
    const result = await putDocument(kbId, key, body, { signal });
    if (!signal.aborted) onUploaded(result);
    return result;
  };

  const uploadFiles = async () => {
    const metadata = parseMetadata(metadataText);
    if (!metadata.ok) {
      setError(metadata.reason);
      return;
    }
    setError(null);
    setBusy(true);
    const signal = startBatch();
    try {
      for (const item of items) {
        if (signal.aborted) return;
        if (item.status !== "ready" && item.status !== "failed") continue;
        if (!item.file || !item.key || !item.mimeType) continue;
        update(item.id, { status: "uploading", message: null });
        try {
          const content = await readFileAsText(item.file);
          if (signal.aborted) return;
          const result = await send(item.key, { title: item.title, content, mime_type: item.mimeType, metadata: metadata.value }, signal);
          update(item.id, result.unchanged ? { status: "unchanged", message: "Same content as the stored version; nothing to do." } : { status: "started", message: null });
        } catch (reason: unknown) {
          if (signal.aborted || isAbort(reason)) return;
          update(item.id, { status: "failed", message: reason instanceof Error ? reason.message : "The upload failed" });
        }
      }
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  };

  const keyForPaste = pasteKey.trim() || documentKeyFrom(pasteTitle) || "";
  const pasteKeyProblem = keyForPaste ? documentKeyProblem(keyForPaste) : "Give the document a key or a title.";

  const uploadPaste = async (event: React.FormEvent) => {
    event.preventDefault();
    setPasteResult(null);
    if (pasteKeyProblem) {
      setPasteResult({ ok: false, message: pasteKeyProblem });
      return;
    }
    if (!pasteContent.trim()) {
      setPasteResult({ ok: false, message: "Paste some text first." });
      return;
    }
    const metadata = parseMetadata(pasteMetadata);
    if (!metadata.ok) {
      setPasteResult({ ok: false, message: metadata.reason });
      return;
    }
    setBusy(true);
    const signal = startBatch();
    try {
      const result = await send(keyForPaste, {
        title: pasteTitle.trim() || null,
        content: pasteContent,
        mime_type: pasteFormat,
        source_url: pasteUrl.trim() || null,
        metadata: metadata.value as Record<string, MetadataValue> | null,
      }, signal);
      setPasteResult({ ok: true, message: result.unchanged ? `${keyForPaste}: unchanged, nothing to do.` : `${keyForPaste}: stored, ingest started.` });
      if (!result.unchanged) setPasteContent("");
    } catch (reason: unknown) {
      if (signal.aborted || isAbort(reason)) return;
      setPasteResult({ ok: false, message: reason instanceof Error ? reason.message : "The upload failed" });
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  };

  const pending = items.filter((item) => item.status === "ready" || item.status === "failed").length;

  return (
    <Sheet open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next); }}>
      {open ? (
        <SheetContent size="md" data-testid="knowledge-upload-sheet">
          <SheetHeader title="Upload documents" description={`To ${kbName}. Markdown, text or HTML, read as UTF-8; re-uploading a key replaces that document.`} />
          <SheetBody>
            <Tabs defaultValue="files" className="flex min-w-0 flex-col gap-4">
              <TabsList aria-label="Upload source">
                <TabsTrigger value="files" data-testid="knowledge-upload-tab-files">Files</TabsTrigger>
                <TabsTrigger value="paste" data-testid="knowledge-upload-tab-paste">Paste text</TabsTrigger>
              </TabsList>

              <TabsContent value="files" className="flex min-w-0 flex-col gap-4">
                <div
                  data-testid="knowledge-upload-dropzone"
                  data-dragging={dragging || undefined}
                  onDragOver={(event) => {
                    event.preventDefault();
                    setDragging(true);
                  }}
                  onDragLeave={() => setDragging(false)}
                  onDrop={(event) => {
                    event.preventDefault();
                    setDragging(false);
                    addFiles(event.dataTransfer?.files ?? null);
                  }}
                  className={cn(
                    "flex min-w-0 flex-col items-center gap-2 rounded-lg border-2 border-dashed border-border p-6 text-center",
                    dragging ? "border-ring bg-fill-hover" : "bg-surface",
                  )}
                >
                  <Upload className="size-6 text-muted-foreground" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                  <p className="text-body text-foreground">Drop .md, .txt or .html files here</p>
                  <input
                    ref={inputRef}
                    id={`${id}-files`}
                    type="file"
                    multiple
                    accept={UPLOAD_ACCEPT}
                    className="sr-only"
                    tabIndex={-1}
                    data-testid="knowledge-upload-input"
                    onChange={(event) => {
                      addFiles(event.target.files);
                      event.target.value = "";
                    }}
                  />
                  <Button type="button" size="sm" onClick={() => inputRef.current?.click()} disabled={busy}>Choose files</Button>
                  <label htmlFor={`${id}-files`} className="sr-only">Choose files to upload</label>
                </div>

                {items.length > 0 ? (
                  <ul className="flex min-w-0 flex-col divide-y divide-border rounded-md border border-border" data-testid="knowledge-upload-files">
                    {items.map((item) => (
                      <li key={item.id} data-testid="knowledge-upload-file" data-status={item.status} className="flex min-w-0 flex-col gap-1 px-3 py-2">
                        <span className="flex min-w-0 items-center gap-2">
                          <span className="min-w-0 flex-1 truncate font-mono text-label" title={item.name}>{item.name}</span>
                          <Badge variant={STATUS_VARIANT[item.status]}>{STATUS_LABEL[item.status]}</Badge>
                          {item.status === "ready" || item.status === "refused" ? (
                            <button
                              type="button"
                              className="rounded-sm text-caption text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                              onClick={() => setItems((current) => current.filter((other) => other.id !== item.id))}
                              aria-label={`Remove ${item.name}`}
                              disabled={busy}
                            >
                              Remove
                            </button>
                          ) : null}
                        </span>
                        {item.message ? (
                          <span className={cn("text-caption", item.status === "refused" || item.status === "failed" ? "text-destructive-text" : "text-muted-foreground")} data-testid={item.status === "refused" ? "knowledge-upload-refused" : undefined}>
                            {item.message}
                          </span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : null}

                <div className="space-y-1">
                  <label htmlFor={`${id}-metadata`} className={labelClass}>Metadata for these files <span className="font-normal text-muted-foreground">(optional)</span></label>
                  <textarea id={`${id}-metadata`} data-testid="knowledge-upload-metadata" rows={2} value={metadataText} onChange={(event) => setMetadataText(event.target.value)} className={`${fieldClass} font-mono`} placeholder={"lang: en\nproduct: billing"} />
                  <p className={hintClass}>One <code>key: value</code> per line (up to 16). Agents can filter retrieval on it.</p>
                </div>
                {error ? <p role="alert" className={alertClass}>{error}</p> : null}
              </TabsContent>

              <TabsContent value="paste">
                <form className="flex min-w-0 flex-col gap-3" onSubmit={(event) => void uploadPaste(event)} aria-label="Paste a document" data-testid="knowledge-upload-paste">
                  <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                    <div className="min-w-0 space-y-1">
                      <label htmlFor={`${id}-title`} className={labelClass}>Title</label>
                      <input id={`${id}-title`} data-testid="knowledge-paste-title" value={pasteTitle} maxLength={500} onChange={(event) => setPasteTitle(event.target.value)} className={fieldClass} />
                    </div>
                    <div className="min-w-0 space-y-1">
                      <label htmlFor={`${id}-key`} className={labelClass}>Document key</label>
                      <input id={`${id}-key`} data-testid="knowledge-paste-key" value={pasteKey} maxLength={256} placeholder={documentKeyFrom(pasteTitle) ?? "faq.md"} onChange={(event) => setPasteKey(event.target.value)} className={`${fieldClass} font-mono`} />
                    </div>
                  </div>
                  <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                    <div className="min-w-0 space-y-1">
                      <label htmlFor={`${id}-format`} className={labelClass}>Format</label>
                      <select id={`${id}-format`} data-testid="knowledge-paste-format" value={pasteFormat} onChange={(event) => setPasteFormat(event.target.value)} className={fieldClass}>
                        {PASTE_FORMATS.map((format) => <option key={format.value} value={format.value}>{format.label}</option>)}
                      </select>
                    </div>
                    <div className="min-w-0 space-y-1">
                      <label htmlFor={`${id}-url`} className={labelClass}>Source URL <span className="font-normal text-muted-foreground">(optional)</span></label>
                      <input id={`${id}-url`} data-testid="knowledge-paste-url" type="url" value={pasteUrl} onChange={(event) => setPasteUrl(event.target.value)} className={fieldClass} placeholder="https://docs.example.com/faq" />
                    </div>
                  </div>
                  <div className="min-w-0 space-y-1">
                    <label htmlFor={`${id}-content`} className={labelClass}>Text</label>
                    <textarea id={`${id}-content`} data-testid="knowledge-paste-content" rows={10} value={pasteContent} onChange={(event) => setPasteContent(event.target.value)} className={`${fieldClass} font-mono`} />
                  </div>
                  <div className="min-w-0 space-y-1">
                    <label htmlFor={`${id}-paste-metadata`} className={labelClass}>Metadata <span className="font-normal text-muted-foreground">(optional)</span></label>
                    <textarea id={`${id}-paste-metadata`} rows={2} value={pasteMetadata} onChange={(event) => setPasteMetadata(event.target.value)} className={`${fieldClass} font-mono`} placeholder="lang: en" />
                  </div>
                  {pasteResult ? (
                    <p role={pasteResult.ok ? "status" : "alert"} data-testid="knowledge-paste-result" className={pasteResult.ok ? "text-label text-success-text" : alertClass}>{pasteResult.message}</p>
                  ) : null}
                  <div className="flex justify-end">
                    <Button type="submit" variant="primary" disabled={busy} data-testid="knowledge-paste-submit">{busy ? "Uploading…" : "Upload text"}</Button>
                  </div>
                </form>
              </TabsContent>
            </Tabs>
          </SheetBody>
          <SheetFooter>
            <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>Close</Button>
            <Button variant="primary" onClick={() => void uploadFiles()} disabled={busy || pending === 0} data-testid="knowledge-upload-submit">
              {busy ? "Uploading…" : pending > 0 ? `Upload ${pending} file${pending === 1 ? "" : "s"}` : "Upload files"}
            </Button>
          </SheetFooter>
        </SheetContent>
      ) : null}
    </Sheet>
  );
}
