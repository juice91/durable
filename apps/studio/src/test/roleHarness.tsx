// Renders a component as a viewer, an editor or an admin: the IAM routes answer from `iamFixtures`, the rest from `routes`.
import React from "react";
import { act, render } from "@testing-library/react";
import { vi } from "vitest";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { iamAnswer, type TestRole } from "./iamFixtures";

/** The first pattern that matches `METHOD /path?query` answers; anything else is an empty list. */
export type Routes = [RegExp, unknown][];

export const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

export function stubApi(role: TestRole, routes: Routes = []) {
  const fetch = vi.fn(async (input: string | URL, init?: RequestInit) => {
    const url = String(input);
    const iam = iamAnswer(role, url);
    if (iam) return iam;
    const parsed = new URL(url, "http://studio.test");
    const line = `${init?.method ?? "GET"} ${parsed.pathname}${parsed.search}`;
    const found = routes.find(([pattern]) => pattern.test(line));
    return json(found ? found[1] : []);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

export function renderAs(role: TestRole, element: React.ReactElement, routes: Routes = []) {
  stubApi(role, routes);
  return render(<TooltipProvider>{element}</TooltipProvider>);
}

/** Lets every answer in flight land: an absence asserted afterwards is a refusal, not a control still loading. */
export async function settle(): Promise<void> {
  for (let turn = 0; turn < 5; turn += 1) await act(() => new Promise<void>((resolve) => setTimeout(resolve, 0)));
}
