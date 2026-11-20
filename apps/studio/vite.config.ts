// The self-hosted Mac runs several jobs at once, so a Studio test that needs well under a second on an
// idle machine can miss vitest's 5 s default under full load. Only CI gets the longer budget; a local
// run keeps the default so a genuinely slow test is still obvious.
const CI_TIMEOUT = process.env.CI ? 15000 : undefined;

export default {
  // Next compiles JSX with the automatic runtime (no `import React` needed); tests do the same.
  esbuild: { jsx: "automatic" },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    testTimeout: CI_TIMEOUT,
    hookTimeout: CI_TIMEOUT,
  },
  resolve: {
    alias: {
      "@": "/src",
    },
  },
};
