// Checks the built site: every internal link, image, script and stylesheet resolves to a file, every anchor exists,
// nothing is loaded from another origin (no analytics, no third-party fonts), and no internal product name is shown.
//
//   node scripts/check-links.mjs dist
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { BASE } from "./config.mjs";
import { stripComments } from "./markdown.mjs";

const decode = (s) => s.replace(/&#x([0-9a-f]+);/gi, (_m, h) => String.fromCodePoint(parseInt(h, 16))).replace(/&#(\d+);/g, (_m, d) => String.fromCodePoint(Number(d))).replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
// A tag written inside a comment, a script or a style block is text, not a link. Only the opening tags are kept.
const stripScripts = (html) => stripComments(html).replace(/(<(script|style)\b[^>]*>)[\s\S]*?(<\/\2\b[^>]*>)/gi, "$1$3");

// The repository's historical project name must not be shown on the site (identifiers in lower case are real names of
// things and stay). Written in two parts so that this file does not carry it either.
export const FORBIDDEN = [new RegExp(["Pydantic", "OS"].join(""))];

function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => (entry.isDirectory() ? walk(path.join(dir, entry.name)) : [path.join(dir, entry.name)]));
}

export function parsePage(html) {
  const ids = new Set();
  const links = [];
  const loads = [];
  for (const [tag, name] of stripScripts(html).matchAll(/<([a-zA-Z][\w:-]*)(?:\s(?:[^<>"']|"[^"]*"|'[^']*')*)?>/g)) {
    const attrs = Object.fromEntries([...tag.matchAll(/\s([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)].map((m) => [m[1].toLowerCase(), decode(m[2] ?? m[3])]));
    if (attrs.id) ids.add(attrs.id);
    if (name === "a" && attrs.name) ids.add(attrs.name);
    const loading = name !== "a" && !(name === "link" && !/stylesheet|icon|preload|modulepreload/.test(attrs.rel ?? ""));
    for (const key of ["href", "src"]) {
      if (attrs[key] === undefined) continue;
      if (name === "link" && /canonical|alternate|sitemap/.test(attrs.rel ?? "")) continue;
      links.push(attrs[key]);
      if (loading) loads.push(attrs[key]);
    }
    for (const candidate of (attrs.srcset ?? "").split(",")) {
      const url = candidate.trim().split(/\s+/)[0];
      if (url) links.push(url), loads.push(url);
    }
  }
  return { ids, links, loads };
}

/** @returns {{problems: string[], pages: number, links: number}} */
export function check(dist, base = BASE) {
  const files = walk(dist);
  const present = new Set(files.map((f) => path.relative(dist, f).split(path.sep).join("/")));
  const pages = new Map();
  // The slide deck is copied in as it is (and maintained elsewhere): links into it are checked, its own pages are not.
  for (const file of present) if (file.endsWith(".html") && !file.startsWith("slides/")) pages.set(file, parsePage(fs.readFileSync(path.join(dist, file), "utf8")));
  const slideIds = (file) => parsePage(fs.readFileSync(path.join(dist, file), "utf8")).ids;

  const problems = [];
  let count = 0;
  for (const [file, page] of pages) {
    const html = fs.readFileSync(path.join(dist, file), "utf8");
    for (const word of FORBIDDEN) if (word.test(html)) problems.push(`${file}: contains the forbidden name ${word.source}`);
    for (const url of page.loads) if (/^(https?:)?\/\//i.test(url)) problems.push(`${file}: loads ${url} from another origin`);
    const pageUrl = new URL(`${base}/${file.replace(/(^|\/)index\.html$/, "$1")}`, "http://site.invalid");
    for (const link of page.links) {
      if (/^([a-z][a-z0-9+.-]*:|\/\/)/i.test(link)) continue;
      count++;
      const url = new URL(link, pageUrl);
      const pathname = decodeURIComponent(url.pathname);
      if (base && pathname !== base && !pathname.startsWith(`${base}/`)) {
        problems.push(`${file}: ${link} leaves the base path ${base}`);
        continue;
      }
      const relative = pathname.slice(base.length).replace(/^\//, "");
      const found = [relative, path.posix.join(relative, "index.html")].find((c) => present.has(c));
      if (!found) {
        problems.push(`${file}: ${link} does not exist`);
        continue;
      }
      const anchor = decodeURIComponent(url.hash.slice(1));
      if (anchor && anchor !== "_top" && found.endsWith(".html")) {
        const ids = pages.get(found)?.ids ?? slideIds(found);
        if (!ids.has(anchor)) problems.push(`${file}: ${link} has no anchor #${anchor}`);
      }
    }
  }
  for (const file of present) {
    if (!file.endsWith(".css") || file.startsWith("slides/")) continue;
    const css = fs.readFileSync(path.join(dist, file), "utf8");
    for (const m of css.matchAll(/(?:url\(\s*|@import\s+)["']?((?:https?:)?\/\/[^"')\s;]+)/g)) problems.push(`${file}: loads ${m[1]} from another origin`);
  }
  for (const required of ["index.html", "docs/index.html"]) if (!present.has(required)) problems.push(`${required} is missing`);
  return { problems, pages: pages.size, links: count };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { problems, pages, links } = check(path.resolve(process.argv[2] ?? "dist"));
  for (const problem of problems) console.error(problem);
  console.log(`checked ${links} internal links on ${pages} pages: ${problems.length} problem(s)`);
  process.exit(problems.length ? 1 : 0);
}
