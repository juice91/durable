import fs from "node:fs";
import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";
import { BASE, REPO_URL, SITE_URL } from "./scripts/config.mjs";
import { sidebar } from "./scripts/nav.mjs";

const apiPagesFile = new URL("./src/generated/api-pages.json", import.meta.url);
const apiPages = fs.existsSync(apiPagesFile) ? JSON.parse(fs.readFileSync(apiPagesFile, "utf8")) : [];

export default defineConfig({
  site: SITE_URL,
  base: BASE || "/",
  trailingSlash: "ignore",
  build: { format: "directory" },
  // The landing page imports the screenshots from the repository's docs/ folder.
  vite: { server: { fs: { allow: [".."] } } },
  devToolbar: { enabled: false },
  integrations: [
    starlight({
      title: "Durable",
      description: "A self-hosted platform for durable AI agents and workflows.",
      favicon: "/favicon.svg",
      logo: { src: "./src/mark.svg" },
      social: [{ icon: "github", label: "GitHub", href: REPO_URL }],
      customCss: ["./src/styles/fonts.css", "./src/styles/tokens.css", "./src/styles/docs.css"],
      sidebar: sidebar(apiPages),
      lastUpdated: false,
      tableOfContents: { minHeadingLevel: 2, maxHeadingLevel: 3 },
    }),
  ],
});
