/** True for an absolute http(s) URL: the only external URLs Studio renders as links from data it
 * did not write (knowledge base source URLs, citation sources). No dependencies: the chat imports it. */
export function isSafeHttpUrl(url: string | null | undefined): url is string {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}
