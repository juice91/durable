import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { normalizeBase } from "../scripts/config.mjs";
import { parseDoc, rewriteLinks, rewriteTarget } from "../scripts/markdown.mjs";
import { pages, sidebar } from "../scripts/nav.mjs";

const files = new Set(["docs/a.md", "docs/sub/b.md", "docs/sub/index.md", "docs/sub", "docs/assets/x.png", "docs/notes.md", "README.md", "plugins"]);
const ctx = (from = "a.md") => ({
  from,
  base: "/durable",
  repoUrl: "https://github.com/o/r",
  urlFor: (doc) => ({ "a.md": "/durable/docs/a/", "sub/b.md": "/durable/docs/b/", "sub/index.md": "/durable/docs/sub/" })[doc],
  exists: (p) => files.has(p),
  isDirectory: (p) => p === "docs/sub" || p === "plugins",
});

test("a listed page becomes a site route and keeps its anchor", () => {
  assert.equal(rewriteTarget("sub/b.md#x", ctx()), "/durable/docs/b/#x");
  assert.equal(rewriteTarget("../a.md", ctx("sub/b.md")), "/durable/docs/a/");
  assert.equal(rewriteTarget("sub/", ctx()), "/durable/docs/sub/");
});

test("assets are served by the site, other repository files by GitHub", () => {
  assert.equal(rewriteTarget("assets/x.png", ctx()), "/durable/assets/x.png");
  assert.equal(rewriteTarget("notes.md", ctx()), "https://github.com/o/r/blob/main/docs/notes.md");
  assert.equal(rewriteTarget("../README.md", ctx()), "https://github.com/o/r/blob/main/README.md");
  assert.equal(rewriteTarget("../plugins", ctx()), "https://github.com/o/r/tree/main/plugins");
});

test("a link to nothing fails the build", () => {
  assert.throws(() => rewriteTarget("gone.md", ctx()), /points at nothing/);
  assert.throws(() => rewriteTarget("../../outside", ctx()), /points at nothing/);
});

test("code is left alone", () => {
  const text = "`[x](gone.md)` and [`y`](sub/b.md)\n```md\n[z](gone.md)\n```";
  assert.equal(rewriteLinks(text, ctx()), "`[x](gone.md)` and [`y`](/durable/docs/b/)\n```md\n[z](gone.md)\n```");
});

test("title and description come from the document", () => {
  const doc = parseDoc("<!-- note -->\n# The `Title`\n\nFirst paragraph with a [link](a.md).\n\nSecond.\n", "x");
  assert.equal(doc.title, "The Title");
  assert.equal(doc.description, "First paragraph with a link.");
  assert.ok(!doc.body.includes("# The"));
  assert.ok(!doc.body.includes("note"));
});

test("base paths", () => {
  assert.equal(normalizeBase(undefined), "/durable");
  assert.equal(normalizeBase("/durable/"), "/durable");
  assert.equal(normalizeBase("/"), "");
});

test("every navigation entry exists, once", () => {
  const docs = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../docs");
  const listed = pages();
  for (const p of listed) assert.ok(fs.existsSync(path.join(docs, p.source)), `docs/${p.source} is listed in nav.mjs but missing`);
  assert.equal(new Set(listed.map((p) => p.source)).size, listed.length);
  assert.equal(new Set(listed.map((p) => p.slug)).size, listed.length);
  assert.equal(sidebar([{ slug: "reference/api/runs", label: "Runs" }]).length, sidebar().length);
});

test("the landing page links only to published pages", () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const landing = fs.readFileSync(path.join(here, "../src/pages/index.astro"), "utf8");
  const slugs = new Set(pages().map((p) => p.slug));
  const used = [...landing.matchAll(/docs\("([^"]+)"\)/g)].map((m) => m[1]).concat([...landing.matchAll(/\["[^"]+", "(guides\/[^"]+)",/g)].map((m) => m[1]));
  assert.ok(used.length > 10);
  for (const slug of used) assert.ok(slugs.has(slug), `the landing page links to docs/${slug}, which nav.mjs does not publish`);
});
