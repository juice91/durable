import { useAgUiRuntime } from "@assistant-ui/react-ag-ui";
import { describe, expect, expectTypeOf, it } from "vitest";

import { ResumableHttpAgent } from "./index";

type RuntimeOptions = Parameters<typeof useAgUiRuntime>[0];

describe("AGUI.11 type-level smoke (no rendering)", () => {
  it("exports useAgUiRuntime as a function", () => {
    expect(typeof useAgUiRuntime).toBe("function");
  });

  it("a ResumableHttpAgent is accepted as the useAgUiRuntime agent", () => {
    expectTypeOf<ResumableHttpAgent>().toMatchTypeOf<RuntimeOptions["agent"]>();
    const agent = new ResumableHttpAgent({ url: "http://api.test/agents/a1" });
    const options: RuntimeOptions = { agent };
    expect(options.agent).toBe(agent);
  });
});
