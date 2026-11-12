import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

const componentsDir = path.join(__dirname, "..", "components");
const appDir = path.join(__dirname, "..", "app");

// Files that are allowed to use hardcoded colors (playground components)
const playgroundFiles = new Set([
  "playground",
  "components/playground",
  "src/components/playground",
]);

// Common hardcoded colors to check for - focus on patterns that need theme tokens
const problematicPatterns = [
  "bg-white",
  "border-gray",
  "divide-gray",
  "text-gray-500",
  "text-gray-600",
  "text-gray-700",
  "text-gray-900",
  "text-gray-400",
  "text-gray-800",
  "hover:bg-gray",
  "focus:ring-blue",
];

function isPlaygroundFile(filePath: string): boolean {
  const relPath = path.relative(process.cwd(), filePath);
  return relPath.includes("playground") || relPath.includes("components/playground");
}

function findNonPlaygroundFiles(dir: string, files: string[] = []): string[] {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      findNonPlaygroundFiles(fullPath, files);
    } else if (entry.isFile() && entry.name.endsWith(".tsx")) {
      files.push(fullPath);
    }
  }
  return files;
}

describe("Non-playground components", () => {
  it("should not use hardcoded color classes", () => {
    const allFiles = [...findNonPlaygroundFiles(componentsDir), ...findNonPlaygroundFiles(appDir)];
    
    const violations: Array<{ file: string; lines: Array<{ line: number; content: string }> }> = [];
    
    for (const file of allFiles) {
      if (isPlaygroundFile(file)) continue;
      
      const content = fs.readFileSync(file, "utf-8");
      const lines = content.split("\n");
      const fileViolations: Array<{ line: number; content: string }> = [];
      
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        for (const pattern of problematicPatterns) {
          if (line.includes(pattern)) {
            fileViolations.push({ line: i + 1, content: line.trim() });
          }
        }
      }
      
      if (fileViolations.length > 0) {
        violations.push({ file: path.relative(process.cwd(), file), lines: fileViolations });
      }
    }
    
    if (violations.length > 0) {
      console.log("\nHardcoded color violations found:");
      violations.forEach(({ file, lines }) => {
        console.log(`\n${file}:`);
        lines.forEach(({ line, content }) => {
          console.log(`  Line ${line}: ${content}`);
        });
      });
      expect(violations).toHaveLength(0);
    }
  });
});
