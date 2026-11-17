import { ApiError, BASE_URL, apiErrorFromResponse, apiHeaders } from "@/lib/api";

// Chat attachments (W2.4): files uploaded to POST /attachments, then sent in a message as AG-UI
// content parts whose source is the attachment's URL.

export interface Attachment {
  id: string;
  name: string;
  media_type: string;
  size: number;
}

/** What a model takes as input (an item of GET /models). `source` says how sure that is. */
export interface ModelCapabilities {
  image: boolean;
  pdf: boolean;
  /** "assumed": a guess from the provider, not a fact; the server decides at send. */
  source: "gateway" | "static" | "assumed";
}

const IMAGE_ACCEPT = "image/png,image/jpeg,image/gif,image/webp";
const TEXT_ACCEPT = "text/plain,text/markdown,text/csv,application/json,.md,.csv,.txt,.json";

/** What the API accepts (kept in step with eap_run_store.chat_attachments). */
export const ATTACHMENT_ACCEPT = `${IMAGE_ACCEPT},application/pdf,${TEXT_ACCEPT}`;

/** The picker's accept list for a model: no images when it takes none, no PDF when it takes none.
 * Text is always listed. Without capabilities (a free-typed model), or with only assumed ones (a
 * guess must not hide a file type: the runtime also enforces this list), everything is. */
export function acceptFor(capabilities?: ModelCapabilities | null): string {
  if (!capabilities || capabilities.source === "assumed") return ATTACHMENT_ACCEPT;
  return [capabilities.image ? IMAGE_ACCEPT : "", capabilities.pdf ? "application/pdf" : "", TEXT_ACCEPT]
    .filter(Boolean)
    .join(",");
}
export const ATTACHMENT_TYPES: readonly string[] = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "application/pdf",
  "text/plain",
  "text/markdown",
  "text/csv",
  "application/json",
];
/** The API's default limit (EAP_CHAT_ATTACHMENT_MAX_BYTES); the server has the last word (413). */
export const ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024;

const EXTENSION_TYPES: Record<string, string> = { md: "text/markdown", csv: "text/csv", txt: "text/plain", json: "application/json" };

/** The file's media type; browsers leave `type` empty for Markdown and some CSV files. */
export function fileMediaType(file: Pick<File, "name" | "type">): string {
  if (file.type) return file.type.split(";")[0].trim().toLowerCase();
  const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
  return EXTENSION_TYPES[extension] ?? "";
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 102.4) / 10} KB`;
  return `${Math.round(bytes / (1024 * 102.4)) / 10} MB`;
}

/** The kind of input a file needs from the model, or null for text (always accepted). */
function neededInput(file: Pick<File, "name" | "type">): { key: "image" | "pdf"; label: string } | null {
  const type = fileMediaType(file);
  if (type === "application/pdf") return { key: "pdf", label: "PDF" };
  if (type.startsWith("image/")) return { key: "image", label: "image" };
  return null;
}

/** For a file the model does not take: "no" or "may not" by how the capability is known. */
function modelCopy(file: Pick<File, "name" | "type">, capabilities?: ModelCapabilities | null): { text: string; definite: boolean } | null {
  const need = neededInput(file);
  if (!capabilities || !need || capabilities[need.key]) return null;
  const definite = capabilities.source !== "assumed";
  return { text: `This model ${definite ? "does not" : "may not"} accept ${need.label} files.`, definite };
}

/** A non-blocking warning: the model is only assumed not to take the file (the upload is allowed). */
export function attachmentNotice(file: Pick<File, "name" | "type">, capabilities?: ModelCapabilities | null): string | null {
  const copy = modelCopy(file, capabilities);
  return copy && !copy.definite ? copy.text : null;
}

/** A reason to refuse the file before uploading it, or null. */
export function attachmentRefusal(file: Pick<File, "name" | "type" | "size">, capabilities?: ModelCapabilities | null): string | null {
  if (!ATTACHMENT_TYPES.includes(fileMediaType(file))) {
    return `${file.name} is not a supported file type. Attach an image, a PDF or a text file.`;
  }
  const copy = modelCopy(file, capabilities);
  if (copy?.definite) return copy.text;
  if (file.size === 0) return `${file.name} is empty.`;
  if (file.size > ATTACHMENT_MAX_BYTES) {
    return `${file.name} is too large (${formatBytes(file.size)}). The limit is ${formatBytes(ATTACHMENT_MAX_BYTES)}.`;
  }
  return null;
}

/** The URL a message part references (relative: the API origin is the reader's). */
export function attachmentPath(id: string): string {
  return `/api/v1/attachments/${encodeURIComponent(id)}`;
}

/** The attachment id inside a part URL (relative or absolute), or null. */
export function attachmentIdFromUrl(url: string): string | null {
  const found = /attachments\/([0-9a-fA-F-]{36})(?:[?#].*)?$/.exec(url);
  return found ? found[1].toLowerCase() : null;
}

export async function uploadAttachment(file: File, signal?: AbortSignal): Promise<Attachment> {
  const refusal = attachmentRefusal(file);
  if (refusal) throw new ApiError(0, refusal);
  const form = new FormData();
  form.append("file", file, file.name);
  const response = await fetch(`${BASE_URL}/api/v1/attachments`, { method: "POST", headers: apiHeaders(), body: form, signal });
  if (!response.ok) {
    if (response.status === 413) throw new ApiError(413, `${file.name} is too large for the server's attachment limit.`);
    throw await apiErrorFromResponse(response);
  }
  return (await response.json()) as Attachment;
}

/** The file's bytes (the route needs the bearer, so an <img src> cannot fetch it). */
export async function fetchAttachmentBlob(urlOrId: string, signal?: AbortSignal): Promise<Blob> {
  const path = urlOrId.includes("/") ? urlOrId : attachmentPath(urlOrId);
  const url = /^https?:/i.test(path) ? path : `${BASE_URL}${path}`;
  const response = await fetch(url, { headers: apiHeaders(), signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.blob();
}

/** Discard an upload no message references (DELETE /attachments/{id}). Best effort: the caller
 * ignores failure, because an unsent upload expires on its own (24 h). */
export async function deleteAttachment(id: string): Promise<void> {
  const response = await fetch(`${BASE_URL}${attachmentPath(id)}`, { method: "DELETE", headers: apiHeaders() });
  if (!response.ok && response.status !== 404) throw await apiErrorFromResponse(response);
}
