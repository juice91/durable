export default {
  // Next compiles JSX with the automatic runtime (no `import React` needed); tests do the same.
  esbuild: { jsx: "automatic" },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
  resolve: {
    alias: {
      "@": "/src",
    },
  },
};
