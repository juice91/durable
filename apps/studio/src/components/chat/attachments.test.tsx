import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AssistantRuntimeProvider, ComposerPrimitive, MessagePrimitive, ThreadPrimitive, useExternalStoreRuntime, useLocalRuntime, type ChatModelAdapter, type ThreadMessageLike } from "@assistant-ui/react";
import { ATTACHMENT_MAX_BYTES, attachmentIdFromUrl, attachmentRefusal, type Attachment } from "@/lib/attachments";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { setWindowWidth } from "@/components/frame/frameTesting";
import { createAttachmentAdapter } from "./attachmentAdapter";
import { AttachButton, ComposerAttachmentList, MessageAttachmentList } from "./Attachments";

const ID = "11111111-1111-4111-8111-111111111111";

function png(name = "pic.png", size = 8): File {
  return new File([new Uint8Array(size)], name, { type: "image/png" });
}

let upload: ReturnType<typeof vi.fn<(file: File) => Promise<Attachment>>>;
let received: unknown[];

function Harness() {
  const model = React.useMemo<ChatModelAdapter>(
    () => ({
      async *run({ messages }) {
        received.push(...messages);
        yield { content: [{ type: "text", text: "ok" }] };
      },
    }),
    [],
  );
  const attachments = React.useMemo(() => createAttachmentAdapter((file) => upload(file)), []);
  const runtime = useLocalRuntime(model, { adapters: { attachments } });
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ComposerPrimitive.Root>
        <ComposerAttachmentList />
        <ComposerPrimitive.Input aria-label="Message" />
        <AttachButton />
        <ComposerPrimitive.Send aria-label="Send message" />
      </ComposerPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}

function attach(...files: File[]) {
  fireEvent.change(screen.getByTestId("composer-attach-input"), { target: { files } });
}

beforeEach(() => {
  received = [];
  upload = vi.fn(async (file: File) => ({ id: ID, name: file.name, media_type: file.type, size: file.size }));
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => undefined }));
});
afterEach(() => vi.unstubAllGlobals());

describe("attachment rules", () => {
  it("accepts images, PDFs and text and refuses others and oversized files", () => {
    expect(attachmentRefusal({ name: "a.png", type: "image/png", size: 5 })).toBeNull();
    expect(attachmentRefusal({ name: "n.md", type: "", size: 5 })).toBeNull();
    expect(attachmentRefusal({ name: "x.exe", type: "application/x-msdownload", size: 5 })).toMatch(/not a supported file type/);
    expect(attachmentRefusal({ name: "big.png", type: "image/png", size: ATTACHMENT_MAX_BYTES + 1 })).toMatch(/too large .*limit is 10 MB/);
  });

  it("reads the id out of a part URL", () => {
    expect(attachmentIdFromUrl(`/api/v1/attachments/${ID}`)).toBe(ID);
    expect(attachmentIdFromUrl(`http://x/api/v1/attachments/${ID}?a=1`)).toBe(ID);
    expect(attachmentIdFromUrl("/elsewhere")).toBeNull();
  });
});

describe("the composer attachments", () => {
  it("attaching a file uploads it and shows a chip", async () => {
    render(<Harness />);
    attach(png());
    const chip = await screen.findByTestId("composer-attachment");
    expect(chip).toHaveTextContent("pic.png");
    expect(upload).toHaveBeenCalledTimes(1);
  });

  it("a chip can be removed before sending", async () => {
    render(<Harness />);
    attach(png());
    await screen.findByTestId("composer-attachment");
    fireEvent.click(screen.getByRole("button", { name: "Remove pic.png" }));
    await waitFor(() => expect(screen.queryByTestId("composer-attachment")).toBeNull());
  });

  it("an oversized file shows its error and is neither uploaded nor attached", async () => {
    render(<Harness />);
    attach(png("huge.png", ATTACHMENT_MAX_BYTES + 1));
    expect(await screen.findByRole("alert")).toHaveTextContent(/huge\.png is too large/);
    expect(upload).not.toHaveBeenCalled();
    expect(screen.queryByTestId("composer-attachment")).toBeNull();
  });

  it("an unsupported type shows its error", async () => {
    render(<Harness />);
    attach(new File(["x"], "run.exe", { type: "application/x-msdownload" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/not a supported file type/);
  });

  it("a server refusal (413) shows an error and leaves no chip", async () => {
    upload.mockRejectedValueOnce(new Error("pic.png is too large for the server's attachment limit."));
    render(<Harness />);
    attach(png());
    expect(await screen.findByRole("alert")).toHaveTextContent(/too large for the server/);
    expect(screen.queryByTestId("composer-attachment")).toBeNull();
  });

  it("sending hands the runtime the file as a url part and clears the chip", async () => {
    render(<Harness />);
    attach(png());
    await screen.findByTestId("composer-attachment");
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "what is this?" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    });
    await waitFor(() => expect(received.length).toBeGreaterThan(0));
    const sent = received[0] as { attachments: { content: { data: string; mimeType: string; sourceType: string; filename: string }[] }[] };
    expect(sent.attachments[0].content[0]).toMatchObject({
      data: `/api/v1/attachments/${ID}`,
      mimeType: "image/png",
      sourceType: "url",
      filename: "pic.png",
    });
    await waitFor(() => expect(screen.queryByTestId("composer-attachment")).toBeNull());
  });

  it("has no axe violations with a chip", async () => {
    render(<Harness />);
    attach(png());
    await screen.findByTestId("composer-attachment");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  it("sets no fixed width over 375 px", async () => {
    const restore = setWindowWidth(375);
    try {
      render(<Harness />);
      attach(png("a-very-long-file-name-that-would-overflow-a-narrow-screen-if-it-did-not-truncate.png"));
      const chip = await screen.findByTestId("composer-attachment");
      expect(chip.className).toContain("max-w-full");
      expect(chip.querySelector(".truncate")).not.toBeNull();
      const fixed = Array.from(document.body.querySelectorAll<HTMLElement>("[class]")).filter((el) =>
        Array.from(el.classList).some((c) => /^(min-)?w-\[(\d+)px\]$/.test(c) && Number(/\[(\d+)px\]/.exec(c)![1]) > 375),
      );
      expect(fixed).toEqual([]);
    } finally {
      restore();
    }
  });
});

const SENT: ThreadMessageLike = {
  id: "u1",
  role: "user",
  content: [{ type: "text", text: "see these" }],
  attachments: [
    { id: "0", type: "image", name: "pic.png", contentType: "image/png", status: { type: "complete" }, content: [{ type: "image", image: `/api/v1/attachments/${ID}` }] },
    { id: "1", type: "document", name: "notes.pdf", contentType: "application/pdf", status: { type: "complete" }, content: [{ type: "file", data: `/api/v1/attachments/${ID}`, mimeType: "application/pdf", sourceType: "url" }] },
  ],
} as ThreadMessageLike;

function Transcript() {
  const runtime = useExternalStoreRuntime({ messages: [SENT], convertMessage: (m: ThreadMessageLike) => m, onNew: async () => undefined });
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Messages>
        {() => (
          <MessagePrimitive.Root>
            <MessageAttachmentList />
            <MessagePrimitive.Parts />
          </MessagePrimitive.Root>
        )}
      </ThreadPrimitive.Messages>
    </AssistantRuntimeProvider>
  );
}

describe("the transcript", () => {
  it("shows a sent message's files as chips (an image thumbnail, a file chip), as after a reload", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new Blob(["x"], { type: "image/png" }), { status: 200 })));
    process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = "Bearer dev:t:u";
    render(<Transcript />);
    const chips = await screen.findAllByTestId("message-attachment");
    expect(chips.map((c) => c.textContent)).toEqual(["pic.png", "notes.pdf"]);
    expect(chips[0]).toHaveAttribute("data-attachment-id", ID);
    await waitFor(() => expect(chips[0].querySelector("img")).not.toBeNull());
    expect(chips[1].querySelector("img")).toBeNull();
  });
});
