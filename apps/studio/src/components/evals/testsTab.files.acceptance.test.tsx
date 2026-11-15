/**
 * AC.16 acceptance: exporting a suite as a pydantic_evals dataset file and importing one back
 * (round trip, and an invalid file showing the API's 422), against a fake of the AC.12 API.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { nextRouter } from "@/test/nextRouterMock";
import { routes } from "@/lib/routes";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { TestsTab } from "./TestsTab";
import { AGENT, SUITE, callsTo, installFakeApi, jsonOf, suiteOf, yamlOf, type FakeApi } from "./evalsFakeApi";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

let fake: FakeApi;
const downloads: { name: string; blob: Blob }[] = [];
let pending: Blob | null = null;
beforeEach(() => {
  fake = installFakeApi();
  downloads.length = 0;
  URL.createObjectURL = vi.fn((blob: Blob) => {
    pending = blob;
    return "blob:fake";
  });
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    downloads.push({ name: this.download, blob: pending! });
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  fake.restore();
});

function renderSuite() {
  nextRouter.reset([routes.agentTests(AGENT, { suite: SUITE })]);
  return render(
    <TooltipProvider>
      <TestsTab agentId={AGENT} />
    </TooltipProvider>,
  );
}
const textOf = (blob: Blob) =>
  new Promise<string>((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.readAsText(blob);
  });
async function exportAs(format: "yaml" | "json") {
  const trigger = await screen.findByTestId("eval-suite-export");
  await waitFor(() => expect(trigger).toBeEnabled());
  fireEvent.keyDown(trigger, { key: "Enter" });
  fireEvent.click(await screen.findByTestId(`eval-suite-export-${format}`));
  await waitFor(() => expect(downloads).toHaveLength(1));
  return { name: downloads[0].name, text: await textOf(downloads[0].blob) };
}
async function importFile(text: string, name: string) {
  const input = await screen.findByTestId("eval-suite-import-file");
  await waitFor(() => expect(screen.getByTestId("eval-suite-import")).toBeEnabled());
  const file = new File([text], name);
  // jsdom's File has no text(); a browser's does.
  Object.defineProperty(file, "text", { value: () => Promise.resolve(text) });
  fireEvent.change(input, { target: { files: [file] } });
}
const importCalls = () => callsTo(fake.state, "POST", /import$/);

describe("export", () => {
  it("downloads the dataset as YAML under the server's file name", async () => {
    renderSuite();
    const file = await exportAs("yaml");
    expect(file.name).toBe("Refunds.yaml");
    expect(file.text).toBe(yamlOf(suiteOf()));
    expect(callsTo(fake.state, "GET", /export$/)[0].query.get("format")).toBe("yaml");
  });

  it("downloads JSON on request", async () => {
    renderSuite();
    const file = await exportAs("json");
    expect(file.name).toBe("Refunds.json");
    expect(JSON.parse(file.text).cases.map((c: { name: string }) => c.name)).toEqual(["first", "second"]);
    expect(callsTo(fake.state, "GET", /export$/)[0].query.get("format")).toBe("json");
  });

  it("shows the API error when the export fails", async () => {
    fake.fail({ method: "GET", path: /export$/, status: 500, body: { detail: "export failed" } });
    renderSuite();
    const trigger = await screen.findByTestId("eval-suite-export");
    await waitFor(() => expect(trigger).toBeEnabled());
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(await screen.findByTestId("eval-suite-export-yaml"));
    expect(await screen.findByTestId("eval-suite-action-error")).toHaveTextContent("export failed");
    expect(downloads).toHaveLength(0);
  });
});

describe("import", () => {
  it("round-trips a YAML export: the exact bytes go back, cases are upserted, the list refreshes", async () => {
    renderSuite();
    const exported = await exportAs("yaml");
    fake.state.suites[0].cases = [fake.state.suites[0].cases[0]];
    cleanup();
    renderSuite();
    await waitFor(() => expect(screen.getAllByTestId("eval-case-row")).toHaveLength(1));
    await importFile(exported.text, exported.name);
    await waitFor(() => expect(importCalls()).toHaveLength(1));
    expect(importCalls()[0].raw).toBe(exported.text);
    expect(importCalls()[0].query.get("format")).toBe("yaml");
    expect(importCalls()[0].headers["content-type"]).toBe("application/yaml");
    await waitFor(() => expect(screen.getAllByTestId("eval-case-row")).toHaveLength(2));
    expect(screen.queryByTestId("eval-suite-import-error")).not.toBeInTheDocument();
  });

  it("a .json file is sent as JSON", async () => {
    renderSuite();
    await importFile(jsonOf(suiteOf()), "refunds.json");
    await waitFor(() => expect(importCalls()).toHaveLength(1));
    expect(importCalls()[0].query.get("format")).toBe("json");
    expect(importCalls()[0].headers["content-type"]).toBe("application/json");
  });

  it("shows the API's 422 for an invalid file, and the suite is unchanged", async () => {
    renderSuite();
    await waitFor(() => expect(screen.getAllByTestId("eval-case-row")).toHaveLength(2));
    await importFile("not a dataset", "broken.yaml");
    const error = await screen.findByTestId("eval-suite-import-error");
    expect(error).toHaveTextContent("Import failed: the suite file is invalid at cases: a list of cases is required");
    expect(screen.getAllByTestId("eval-case-row")).toHaveLength(2);
  });

  it("joins a 422 list of unstorable strings into the message", async () => {
    fake.fail({ method: "POST", path: /import$/, status: 422, body: { detail: [{ loc: ["cases", 0, "inputs"], msg: "contains a NUL character" }, { loc: ["cases", 1, "name"], msg: "is too long" }] } });
    renderSuite();
    await importFile(jsonOf(suiteOf()), "x.json");
    expect(await screen.findByTestId("eval-suite-import-error")).toHaveTextContent("contains a NUL character, is too long");
  });

  it("refuses a file over 1 MiB before sending anything", async () => {
    renderSuite();
    await importFile(`cases:\n${"x".repeat(1024 * 1024 + 1)}`, "big.yaml");
    expect(await screen.findByTestId("eval-suite-import-error")).toHaveTextContent("at most 1 MiB");
    expect(importCalls()).toHaveLength(0);
  });

  it("clears the previous import error on the next attempt", async () => {
    renderSuite();
    await importFile("junk", "a.yaml");
    await screen.findByTestId("eval-suite-import-error");
    await importFile(yamlOf(suiteOf()), "b.yaml");
    await waitFor(() => expect(screen.queryByTestId("eval-suite-import-error")).not.toBeInTheDocument());
  });

  it("the Import button opens the file picker", async () => {
    renderSuite();
    const click = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => undefined);
    const button = await screen.findByTestId("eval-suite-import");
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(click).toHaveBeenCalled();
    expect(screen.getByTestId("eval-suite-import-file")).toHaveAttribute("accept", expect.stringContaining(".yaml"));
  });
});
