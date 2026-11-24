// The self-hosted Mac runs several jobs at once: up to four Studio shards plus the heavy job share
// about ten cores. Vitest starts cores-1 workers per run, so four shards put ~36 jsdom workers on
// 10 cores. Measured with four shards at once (CI=1): default workers gave 5 failures, tests up to
// 46 s and 420 tests over 3 s in 217 s; --maxWorkers=3 gave 0 failures, a 12 s worst case and
// 34 tests over 3 s in 169 s. Oversubscription, not the individual tests, caused the timeouts.
// VITEST_MAX_WORKERS overrides the cap (a runner with more cores, or one shard running alone).
const CI_MAX_WORKERS = Number(process.env.VITEST_MAX_WORKERS) || 3;

// A test that needs well under a second on an idle machine can still miss vitest's 5 s default
// under load, on CI or on a laptop running several suites. 15 s is a ceiling, not a target:
// findBy/waitFor (asyncUtilTimeout in src/test/setup.ts) stay below it, so they fail with their own
// message before the test timeout does.
const TEST_TIMEOUT = 15000;

export default {
  // Next compiles JSX with the automatic runtime (no `import React` needed); tests do the same.
  esbuild: { jsx: "automatic" },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    testTimeout: TEST_TIMEOUT,
    hookTimeout: TEST_TIMEOUT,
    // Locally vitest keeps its own default (cores-1): one run owns the machine.
    ...(process.env.CI ? { maxWorkers: CI_MAX_WORKERS } : {}),
  },
  resolve: {
    alias: {
      "@": "/src",
    },
  },
};
