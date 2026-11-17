import type { AttachmentAdapter, CompleteAttachment, PendingAttachment } from "@assistant-ui/react";
import {
  acceptFor,
  attachmentPath,
  attachmentRefusal,
  fileMediaType,
  uploadAttachment,
  type Attachment,
  type ModelCapabilities,
} from "@/lib/attachments";

/**
 * The chat composer's attachment adapter (W2.4). `add` checks the file (type, size) and uploads it
 * (POST /attachments), so a refusal shows when the file is attached, not when the message is sent;
 * `send` turns the uploaded file into the message part the AG-UI runtime sends: a `file` part whose
 * data is the attachment's URL (it becomes an AG-UI `image` or `document` part with a url source).
 * `capabilities` is read at each attach, so the picker's file types and the refusals follow the
 * model chosen now (the adapter is made once per chat).
 */
export function createAttachmentAdapter(
  upload: (file: File) => Promise<Attachment> = uploadAttachment,
  capabilities: () => ModelCapabilities | null | undefined = () => null,
): AttachmentAdapter {
  const uploaded = new Map<string, Attachment>();
  return {
    get accept() {
      return acceptFor(capabilities());
    },
    async *add({ file }) {
      const refusal = attachmentRefusal(file, capabilities());
      if (refusal) throw new Error(refusal);
      const mediaType = fileMediaType(file);
      const base = {
        id: crypto.randomUUID(),
        type: mediaType.startsWith("image/") ? ("image" as const) : ("document" as const),
        name: file.name,
        contentType: mediaType,
        file,
      };
      // Uploaded before the chip exists, so a refusal (413, type) leaves nothing to remove; the
      // paperclip shows the upload in progress.
      const stored = await upload(file);
      uploaded.set(base.id, stored);
      yield { ...base, status: { type: "requires-action", reason: "composer-send" } } as PendingAttachment;
    },
    async remove(attachment) {
      // The upload stays on the server unreferenced; it is only reachable by its id.
      uploaded.delete(attachment.id);
    },
    async send(attachment): Promise<CompleteAttachment> {
      const stored = uploaded.get(attachment.id);
      if (!stored) throw new Error(`${attachment.name} was not uploaded`);
      return {
        ...attachment,
        status: { type: "complete" },
        content: [
          {
            type: "file",
            data: attachmentPath(stored.id),
            mimeType: stored.media_type,
            filename: stored.name,
            sourceType: "url",
          } as CompleteAttachment["content"][number],
        ],
      };
    },
  };
}
