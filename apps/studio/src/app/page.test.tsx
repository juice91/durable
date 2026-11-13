// `/` is a redirect since SL.7 (the dashboard folded into the Metrics Overview).
import { describe, expect, it, vi } from "vitest";
import RootPage from "./page";
import { legacyTarget, routes } from "@/lib/routes";

const redirect = vi.hoisted(() =>
  vi.fn((url: string) => {
    throw new Error(`NEXT_REDIRECT:${url}`);
  }),
);
vi.mock("next/navigation", () => ({ redirect }));

async function target(searchParams: Record<string, string | string[] | undefined>): Promise<string> {
  redirect.mockClear();
  await expect(RootPage({ searchParams: Promise.resolve(searchParams) })).rejects.toThrow("NEXT_REDIRECT:");
  expect(redirect).toHaveBeenCalledTimes(1);
  return redirect.mock.calls[0][0];
}

describe("RootPage (/)", () => {
  it("a bare / opens Agents", async () => {
    expect(await target({})).toBe(routes.agents());
  });

  it("a dashboard link opens Metrics with the same range parameters", async () => {
    expect(await target({ range: "30d" })).toBe(routes.metrics({ range: "30d" }));
    expect(await target({ range: "custom", from: "2026-11-01T00:00:00Z", to: "2026-11-05T00:00:00Z" })).toBe(
      routes.metrics({ range: "custom", from: "2026-11-01T00:00:00Z", to: "2026-11-05T00:00:00Z" }),
    );
  });

  it("interval is not carried (the dashboard ignored it): alone it opens Agents", async () => {
    expect(await target({ interval: "hour" })).toBe(routes.agents());
    expect(await target({ range: "24h", interval: "hour" })).toBe(routes.metrics({ range: "24h" }));
  });

  it("parameters the dashboard did not have are dropped; alone they open Agents", async () => {
    expect(await target({ range: "24h", agent_id: "a-1" })).toBe(routes.metrics({ range: "24h" }));
    expect(await target({ agent_id: "a-1" })).toBe(routes.agents());
  });

  it("follows the legacy table exactly", async () => {
    for (const search of [{}, { range: "90d" }, { from: "x", to: "y" }, { range: ["24h", "7d"] }]) {
      expect(await target(search)).toBe(legacyTarget("/", search));
    }
  });
});
