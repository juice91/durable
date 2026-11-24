// Fails when the built Studio contains a baked dev credential (IAM design 7.1): the public host authenticates with the
// Cloudflare Access cookie and the loopback page gets its bearer at runtime, so no file of the build may hold the string
// below. Usage: `node scripts/check-bundle.mjs [dir]` after `next build` (default: .next).
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const FORBIDDEN = ["Bearer", "dev:"].join(" ");
const root = process.argv[2] ?? ".next";
const SKIP = new Set(["cache"]);
const hits = [];

// One directory listing with entry types and one read per file: no stat-then-read gap for the path to change in.
function walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (SKIP.has(entry.name)) continue;
    const path = join(dir, entry.name);
    if (entry.isDirectory()) walk(path);
    else if (entry.isFile()) {
      const content = readFileSync(path);
      if (content.length < 50 * 1024 * 1024 && content.includes(FORBIDDEN)) hits.push(path);
    }
  }
}

try {
  walk(root);
} catch (error) {
  console.error(`check-bundle: cannot read ${root}: ${error.message}`);
  process.exit(2);
}
if (hits.length > 0) {
  console.error(`check-bundle: the build contains "${FORBIDDEN}":\n  ${hits.join("\n  ")}`);
  process.exit(1);
}
console.log(`check-bundle: no "${FORBIDDEN}" under ${root}`);
