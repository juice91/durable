import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { check, parsePage } from "../scripts/check-links.mjs";
import { renderOpenApi, shape } from "../scripts/openapi.mjs";

function dist(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "links-"));
  for (const [name, text] of Object.entries(files)) {
    fs.mkdirSync(path.join(dir, path.dirname(name)), { recursive: true });
    fs.writeFileSync(path.join(dir, name), text);
  }
  return dir;
}

test("good links, anchors and assets pass", () => {
  const dir = dist({
    "index.html": '<a href="/durable/docs/#a">d</a><a href="docs/">r</a><a href="https://example.com/x">e</a><img src="/durable/i.png">',
    "docs/index.html": '<h2 id="a">A</h2><a href="#a">self</a><a href="../">home</a>',
    "i.png": "",
  });
  assert.deepEqual(check(dir, "/durable").problems, []);
});

test("broken links, anchors, base escapes, third-party loads and forbidden names are reported", () => {
  const dir = dist({
    "index.html": `<a href="/durable/nope/">x</a><a href="/durable/docs/#zz">y</a><a href="/other">z</a><script src="https://cdn.example.com/a.js"></script><p>${["Pydantic", "OS"].join("")}</p>`,
    "docs/index.html": "",
    "a.css": "@font-face{src:url(https://fonts.example.com/f.woff2)}",
  });
  const problems = check(dir, "/durable").problems.join("\n");
  for (const wanted of ["nope", "#zz", "leaves the base path", "from another origin", "forbidden name", "a.css"]) assert.ok(problems.includes(wanted), wanted);
});

test("a tag inside a script is not a link", () => {
  const dir = dist({ "index.html": '<script>"<a href=/missing>"</script>', "docs/index.html": "" });
  assert.deepEqual(check(dir, "/durable").problems, []);
});

test("quoted attribute values may hold angle brackets, and a tag that never closes is read in linear time", () => {
  assert.deepEqual(parsePage(`<a title="a > b < c" href='/x' data-n="it's">x</a>`).links, ["/x"]);
  // A pattern that can split a quoted value in more than one way takes exponential time on this and never returns.
  assert.deepEqual(parsePage(`<A ${'""'.repeat(5000)}`).links, []);
});

test("docstring backslashes cannot undo the Markdown escaping", () => {
  const spec = { paths: { "/x": { get: { description: String.raw`see \[a](b)`, responses: {} } } } };
  assert.ok(renderOpenApi(spec)[0].body.includes(String.raw`see \\\[a](b)`));
});

test("the API reference groups routes by tag", () => {
  const spec = {
    paths: { "/api/v1/things": { post: { tags: ["things"], summary: "Create Thing", requestBody: { content: { "application/json": { schema: { $ref: "#/components/schemas/Thing" } } } }, responses: { 201: {}, 422: {} } } } },
    components: { schemas: { Thing: { type: "object", properties: { name: { type: "string" }, kind: { anyOf: [{ type: "string" }, { type: "null" }] } }, required: ["name"] } } },
  };
  const [page] = renderOpenApi(spec);
  assert.equal(page.slug, "reference/api/things");
  assert.match(page.body, /## Create Thing/);
  assert.match(page.body, /\| `name` \| `string` \| yes \|/);
  assert.match(page.body, /string \\\| null/);
  assert.equal(shape(spec, { type: "array", items: { type: "integer" } }), "integer[]");
});
