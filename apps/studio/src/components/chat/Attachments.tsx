"use client";

import React, { useEffect, useRef, useState } from "react";
import { AttachmentPrimitive, ComposerPrimitive, MessagePrimitive, useAui, useAuiState } from "@assistant-ui/react";
import { FileText, Loader2, Paperclip, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { acceptFor, attachmentIdFromUrl, attachmentRefusal, fetchAttachmentBlob, type ModelCapabilities } from "@/lib/attachments";
import { focusRing } from "@/components/ui/Button";

/** An image's thumbnail: the route needs the bearer, so the bytes are fetched and shown as a blob URL. */
function Thumbnail({ source, file, alt }: { source: string | null; file?: File; alt: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let revoke: string | null = null;
    let cancelled = false;
    const show = (blob: Blob) => {
      if (cancelled) return;
      revoke = URL.createObjectURL(blob);
      setUrl(revoke);
    };
    if (file) show(file);
    else if (source) fetchAttachmentBlob(source).then(show, () => undefined);
    return () => {
      cancelled = true;
      if (revoke) URL.revokeObjectURL(revoke);
    };
  }, [source, file]);
  // eslint-disable-next-line @next/next/no-img-element
  return url ? <img src={url} alt={alt} className="size-8 shrink-0 rounded object-cover" /> : <FileText aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-4 shrink-0 text-muted-foreground" />;
}

const chip = "inline-flex max-w-full min-w-0 items-center gap-1.5 rounded-md border border-border bg-surface px-1.5 py-1 text-caption text-foreground";

/** One attachment in the composer: name, a remove button, and a spinner while it uploads. */
function ComposerChip() {
  const name = useAuiState((s) => s.attachment.name);
  const type = useAuiState((s) => s.attachment.type);
  const file = useAuiState((s) => (s.attachment as { file?: File }).file);
  const uploading = useAuiState((s) => s.attachment.status.type === "running");
  return (
    <AttachmentPrimitive.Root data-testid="composer-attachment" className={chip}>
      {uploading ? (
        <Loader2 aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-4 shrink-0 animate-spin text-muted-foreground" />
      ) : type === "image" ? (
        <Thumbnail source={null} file={file} alt="" />
      ) : (
        <FileText aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-4 shrink-0 text-muted-foreground" />
      )}
      <span className="min-w-0 truncate"><AttachmentPrimitive.Name /></span>
      {uploading ? <span className="sr-only">Uploading</span> : null}
      <AttachmentPrimitive.Remove
        aria-label={`Remove ${name}`}
        className={cn("inline-flex size-5 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-fill-hover hover:text-foreground", focusRing)}
      >
        <X aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-3.5" />
      </AttachmentPrimitive.Remove>
    </AttachmentPrimitive.Root>
  );
}

/** The files waiting to be sent, above the message box (nothing renders without any). */
export function ComposerAttachmentList() {
  const count = useAuiState((s) => s.composer.attachments.length);
  if (count === 0) return null;
  return (
    <ul aria-label="Attachments" data-testid="composer-attachments" className="m-0 flex min-w-0 list-none flex-wrap gap-1.5 p-0 px-1">
      <ComposerPrimitive.Attachments components={{ Attachment: () => <li className="min-w-0 max-w-full"><ComposerChip /></li> }} />
    </ul>
  );
}

/**
 * The paperclip: opens the file picker, which offers what the selected model takes (`capabilities`;
 * all when unknown). A refused file's reason shows under the composer (role=alert). The soft note
 * for a file the model is only assumed not to take (role=status) comes from the attachment adapter,
 * which sees every way of adding a file, and arrives as `notice`.
 */
export function AttachButton({ disabled = false, capabilities = null, notice = null }: { disabled?: boolean; capabilities?: ModelCapabilities | null; notice?: string | null }) {
  const aui = useAui();
  const input = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(0);
  const onFiles = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    setError(null);
    setUploading((n) => n + files.length);
    for (const file of files) {
      try {
        const refusal = attachmentRefusal(file, capabilities);
        if (refusal) throw new Error(refusal);
        await aui.composer().addAttachment(file);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : `${file.name} could not be attached.`);
      } finally {
        setUploading((n) => n - 1);
      }
    }
  };
  return (
    <>
      <input ref={input} type="file" multiple hidden accept={acceptFor(capabilities)} aria-label="Attach files" data-testid="composer-attach-input" onChange={onFiles} />
      <button
        type="button"
        aria-label="Attach files"
        disabled={disabled}
        aria-busy={uploading > 0}
        data-testid="composer-attach-button"
        onClick={() => input.current?.click()}
        className={cn("inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-fill-hover hover:text-foreground disabled:pointer-events-none disabled:opacity-50", focusRing)}
      >
        {uploading > 0 ? (
          <Loader2 aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-icon animate-spin" />
        ) : (
          <Paperclip aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-icon" />
        )}
      </button>
      {notice ? (
        <p role="status" data-testid="composer-attach-notice" className="basis-full text-caption text-muted-foreground">{notice}</p>
      ) : null}
      {error ? (
        <p role="alert" data-testid="composer-attach-error" className="basis-full text-caption text-destructive-text">{error}</p>
      ) : null}
    </>
  );
}

/** One file of a sent message: a thumbnail for an image, else a file chip; it opens the file. */
function MessageChip() {
  const name = useAuiState((s) => s.attachment.name);
  const type = useAuiState((s) => s.attachment.type);
  const part = useAuiState((s) => s.attachment.content?.[0] as { image?: string; data?: string } | undefined);
  const source = part?.image ?? part?.data ?? null;
  const id = source ? attachmentIdFromUrl(source) : null;
  return (
    <AttachmentPrimitive.Root data-testid="message-attachment" data-attachment-id={id ?? undefined} className={chip}>
      {type === "image" ? <Thumbnail source={source} alt={name} /> : <FileText aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-4 shrink-0 text-muted-foreground" />}
      <span className="min-w-0 truncate"><AttachmentPrimitive.Name /></span>
    </AttachmentPrimitive.Root>
  );
}

/** A user message's files (also after a reload: the thread GET returns them as media parts). */
export function MessageAttachmentList() {
  const count = useAuiState((s) => s.message.attachments?.length ?? 0);
  if (count === 0) return null;
  return (
    <div data-testid="message-attachments" className="mb-1.5 flex min-w-0 flex-wrap gap-1.5">
      <MessagePrimitive.Attachments components={{ Attachment: MessageChip }} />
    </div>
  );
}
