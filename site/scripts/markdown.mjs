// Pure helpers for sync-docs.mjs: titles, frontmatter and link rewriting.
import path from "node:path";

const FENCE = /^\s*(```+|~~~+)/;

/** Applies `fn` to every line outside fenced code blocks. */
export function mapOutsideFences(markdown, fn) {
  let fence = null;
  return markdown
    .split("\n")
    .map((line) => {
      const match = FENCE.exec(line);
      if (match) {
        if (fence === null) fence = match[1];
        else if (match[1].startsWith(fence)) fence = null;
        return line;
      }
      return fence === null ? fn(line) : line;
    })
    .join("\n");
}

export function plainText(markdown) {
  return markdown
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[`*]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** Removes HTML comments (the export's region markers among them), repeating until none is left. */
export function stripComments(text) {
  let previous;
  let current = text;
  do {
    previous = current;
    current = current.replace(/<!--[\s\S]*?--!?>\n?/g, "");
  } while (current !== previous);
  return current;
}

/** The first H1 becomes the title (and leaves the body); the first prose paragraph becomes the description. */
export function parseDoc(markdown, fallbackTitle) {
  const lines = stripComments(markdown.replace(/\r\n/g, "\n")).split("\n");
  let title = fallbackTitle;
  let fenced = false;
  for (let i = 0; i < lines.length; i++) {
    if (FENCE.test(lines[i])) fenced = !fenced;
    if (!fenced && lines[i].startsWith("# ")) {
      title = plainText(lines[i].slice(2));
      lines.splice(i, 1);
      break;
    }
  }
  const body = lines.join("\n").replace(/^\s*\n/, "");
  const prose = body
    .split(/\n\s*\n/)
    .find((block) => block.trim() && !/^\s*(#|\||>|[-*] |\d+\. |<|```|~~~)/.test(block));
  let description = plainText(prose ?? "");
  if (description.length > 180) description = `${description.slice(0, 180).replace(/\s+\S*$/, "")}…`;
  return { title, description, body };
}

/**
 * Rewrites one link target found in docs/<from>.
 * Links to listed pages become site routes, files under docs/assets are served by the site, and everything else in the
 * repository points at GitHub. A target that does not exist is an error, so a renamed file breaks the build.
 */
export function rewriteTarget(target, ctx) {
  if (/^([a-z][a-z0-9+.-]*:|#|\/\/|\/)/i.test(target)) return target;
  const hashAt = target.indexOf("#");
  const file = hashAt === -1 ? target : target.slice(0, hashAt);
  const hash = hashAt === -1 ? "" : target.slice(hashAt);
  if (!file) return target;
  const repoPath = path.posix.normalize(path.posix.join("docs", path.posix.dirname(ctx.from), file)).replace(/\/$/, "");
  if (repoPath.startsWith("..") || !ctx.exists(repoPath)) {
    throw new Error(`docs/${ctx.from}: link to ${target} points at nothing in the repository`);
  }
  const directory = ctx.isDirectory(repoPath);
  const docPath = directory ? `${repoPath}/index.md` : repoPath;
  const url = docPath.startsWith("docs/") ? ctx.urlFor(docPath.slice("docs/".length)) : undefined;
  if (url !== undefined) return url + hash;
  if (repoPath.startsWith("docs/assets/")) return `${ctx.base}/${repoPath.slice("docs/".length)}${hash}`;
  return `${ctx.repoUrl}/${directory ? "tree" : "blob"}/main/${repoPath}${hash}`;
}

/** Rewrites inline links `[text](target)`, images and reference definitions outside code. */
export function rewriteLinks(markdown, ctx) {
  // A link's text may wrap onto the next line, so only the `](target)` part is matched, line by line.
  return mapOutsideFences(markdown, (line) => {
    const definition = /^(\s*\[[^\]]+\]:\s+)(\S+)(.*)$/.exec(line);
    if (definition) return definition[1] + rewriteTarget(definition[2], ctx) + definition[3];
    // Odd segments are inline code spans: a link written inside backticks is left alone. A backtick inside a link's
    // text splits the line too, which is fine because the target stays in an even segment.
    return line
      .split(/(`[^`]*`)/)
      .map((part, i) =>
        i % 2
          ? part
          : part.replace(/(\]\()([^)\s]+)((?:\s+"[^"]*")?\))/g, (_m, open, target, close) => open + rewriteTarget(target, ctx) + close),
      )
      .join("");
  });
}

export function frontmatter(data) {
  return ["---", ...Object.entries(data).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => `${k}: ${JSON.stringify(v)}`), "---", ""].join("\n");
}
