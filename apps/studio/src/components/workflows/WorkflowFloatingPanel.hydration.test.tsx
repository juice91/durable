import React from "react";
import { act } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { hydrateRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkflowFloatingPanel, type WorkflowLayoutControl } from "./WorkflowFloatingPanel";
import { DEFAULT_LAYOUT_PREFS, PANEL_DEFAULT_WIDTH } from "./workflowLayout";

function control(panel: "floating" | "sheet", panelCollapsed = false): WorkflowLayoutControl {
  return {
    layout: { panel, panelWidth: PANEL_DEFAULT_WIDTH, leftOccupied: panel === "sheet" ? 0 : PANEL_DEFAULT_WIDTH, stepDetail: "floating" },
    canvasWidth: 1200,
    prefs: { ...DEFAULT_LAYOUT_PREFS, panelCollapsed },
    update: () => undefined,
  };
}

const ui = (c: WorkflowLayoutControl) => (
  <div className="relative">
    <WorkflowFloatingPanel control={c} title="Run details">
      <p data-testid="panel-child">content</p>
    </WorkflowFloatingPanel>
  </div>
);

let root: Root | null = null;
afterEach(() => {
  act(() => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

/** Renders `element` as the server would (no `document` at all), then hydrates it in the browser. */
async function serverRenderThenHydrate(element: React.ReactElement) {
  const realDocument = globalThis.document;
  vi.stubGlobal("document", undefined);
  let html: string;
  try {
    html = renderToString(element);
  } finally {
    vi.stubGlobal("document", realDocument);
    vi.unstubAllGlobals();
  }
  const container = document.createElement("div");
  container.innerHTML = html;
  document.body.appendChild(container);
  const recoverable = vi.fn();
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  await act(async () => {
    root = hydrateRoot(container, element, { onRecoverableError: recoverable });
  });
  const hydrationErrors = consoleError.mock.calls.filter((call) => /hydrat/i.test(String(call[0])));
  consoleError.mockRestore();
  return { container, recoverable, hydrationErrors };
}

describe("WorkflowFloatingPanel hydration (SL.14 re-review)", () => {
  for (const [name, c] of [
    ["floating", control("floating")],
    ["collapsed", control("floating", true)],
    ["bottom Sheet", control("sheet")],
  ] as const) {
    it(`hydrates the ${name} panel without a mismatch`, async () => {
      const { container, recoverable, hydrationErrors } = await serverRenderThenHydrate(ui(c));
      expect(recoverable).not.toHaveBeenCalled();
      expect(hydrationErrors).toEqual([]);
      // The content mounts after hydration: shown in the floating body, held off-page otherwise.
      if (name === "floating") expect(container.querySelector("[data-testid='panel-child']")).not.toBeNull();
      else expect(container.querySelector("[data-testid='panel-child']")).toBeNull();
    });
  }
});
