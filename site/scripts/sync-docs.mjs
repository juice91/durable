// Generates the documentation pages the site builds from: the repository's docs/ folder (the single source of truth)
// and the API reference from the OpenAPI schema. Output is ignored by git.
//
//   SITE_OPENAPI=<file>   use an existing schema file instead of dumping it with uv
//   SITE_OPENAPI=skip     build without the API reference (quick local preview)
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { BASE, REPO_URL } from "./config.mjs";
import { frontmatter, parseDoc, rewriteLinks } from "./markdown.mjs";
import { pages, route } from "./nav.mjs";
import { renderOpenApi } from "./openapi.mjs";

const site = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repo = path.resolve(site, "..");
const content = path.join(site, "src/content/docs");
const generated = path.join(site, "src/generated");

const listed = pages();
const urls = new Map(listed.map((p) => [p.source, `${BASE}/${route(p.slug)}`]));

function write(file, text) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, text);
}

const target = (slug) => path.join(content, "docs", slug ? `${slug}.md` : "index.md");
// "reference/tools" is both a page and a folder of pages: it is written as the folder's index.
const isFolder = (slug) => listed.some((p) => p.slug.startsWith(`${slug}/`));

fs.rmSync(content, { recursive: true, force: true });
fs.rmSync(generated, { recursive: true, force: true });

for (const page of listed) {
  const source = path.join(repo, "docs", page.source);
  const doc = parseDoc(fs.readFileSync(source, "utf8"), page.label);
  const body = rewriteLinks(doc.body, {
    from: page.source,
    base: BASE,
    repoUrl: REPO_URL,
    urlFor: (docPath) => urls.get(docPath),
    exists: (repoPath) => fs.existsSync(path.join(repo, repoPath)),
    isDirectory: (repoPath) => fs.statSync(path.join(repo, repoPath)).isDirectory(),
  });
  const head = frontmatter({ title: doc.title, description: doc.description, editUrl: `${REPO_URL}/edit/main/docs/${page.source}` });
  write(target(page.slug && isFolder(page.slug) ? `${page.slug}/index` : page.slug), head + body);
}

const assets = path.join(repo, "docs/assets");
fs.rmSync(path.join(site, "public/assets"), { recursive: true, force: true });
if (fs.existsSync(assets)) fs.cpSync(assets, path.join(site, "public/assets"), { recursive: true });

let apiPages = [];
let schemaFile = process.env.SITE_OPENAPI;
if (schemaFile !== "skip") {
  if (!schemaFile) {
    schemaFile = path.join(generated, "openapi.json");
    execFileSync("uv", ["run", "--frozen", "--all-packages", "python", "site/scripts/dump-openapi.py", schemaFile], { cwd: repo, stdio: ["ignore", "inherit", "inherit"] });
  }
  apiPages = renderOpenApi(JSON.parse(fs.readFileSync(path.resolve(schemaFile), "utf8")));
  for (const page of apiPages) {
    const body = page.body.replaceAll("IAM_URL", urls.get("iam.md"));
    write(target(page.slug), frontmatter({ title: page.title, description: page.description, editUrl: false }) + body);
  }
}
write(path.join(generated, "api-pages.json"), JSON.stringify(apiPages.map(({ slug, label }) => ({ slug, label }))) + "\n");
console.log(`docs: ${listed.length} pages, ${apiPages.length} API reference pages (base "${BASE || "/"}")`);
