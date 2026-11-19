import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listAdapters: vi.fn(),
  listSources: vi.fn(),
  createSource: vi.fn(),
  updateSource: vi.fn(),
  listSyncs: vi.fn(),
  getSync: vi.fn(),
  listConnections: vi.fn(),
  findSyncWorkflow: vi.fn(),
  publishSyncWorkflow: vi.fn(),
  startSourceSync: vi.fn(),
}));
vi.mock("@/lib/kbSources", async (importOriginal) => ({ ...(await importOriginal<object>()), ...mocks }));

import { SourcesTab } from "./SourcesTab";
import { KbSourceError } from "@/lib/kbSources";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";

const ADAPTERS = [
  { kind: "product_catalog", provider: "acme", distribution: "eap-kb-sources-acme", display_name: "Acme Parts", connection_kind: "oauth2_client_credentials", base_url: "https://api.acme.test", token_url: "https://auth.acme.test/token", config_schema: null },
  { kind: "repository_docs", provider: "github", distribution: "eap-kb-sources", display_name: "GitHub", connection_kind: "api_token", base_url: "https://api.github.com", token_url: null, config_schema: null },
];
const T = "2026-11-18T10:00:00Z";
const SOURCE = {
  id: "s1",
  knowledge_base_id: "kb1",
  connection_id: "c1",
  kind: "product_catalog",
  provider: "acme",
  name: "TI parts",
  enabled: true,
  config: { page_size: 100 },
  last_successful_sync_at: T,
  last_checkpoint_summary: { segments: 3, has_cursor: true },
  last_source_revision: null,
  created_at: T,
  updated_at: T,
};
const CONNECTION = { id: "c1", name: "ti-prod", kind: "oauth2_client_credentials", base_url: "https://api.acme.test", config: { provider: "acme" }, credential_refs: {}, enabled: true, last_checked_at: null, last_status: null, last_error_code: null, created_at: T, updated_at: T };
const SYNC = { run_id: "r-child", source_id: "s1", parent_run_id: "r-parent", mode: "targeted", status: "completed", counters: {}, errors: [], started_at: T, completed_at: T };
const DETAIL = { ...SYNC, report: { discovered: 5, inserted: 2, updated: 1, unchanged: 2, documents_queued: 3, failed: 0, stop_reason: "budget" }, run_status: "completed" };
const WORKFLOW = { id: "w1", name: "KB Source Synchronization", draft_spec: null, latest_version: 1 };

beforeEach(() => {
  mocks.listAdapters.mockReset().mockResolvedValue(ADAPTERS);
  mocks.listSources.mockReset().mockResolvedValue([SOURCE]);
  mocks.createSource.mockReset().mockResolvedValue({ ...SOURCE, id: "s2", name: "New" });
  mocks.updateSource.mockReset().mockResolvedValue({ ...SOURCE, enabled: false });
  mocks.listSyncs.mockReset().mockResolvedValue([SYNC]);
  mocks.getSync.mockReset().mockResolvedValue(DETAIL);
  mocks.listConnections.mockReset().mockResolvedValue([CONNECTION]);
  mocks.findSyncWorkflow.mockReset().mockResolvedValue(WORKFLOW);
  mocks.publishSyncWorkflow.mockReset().mockResolvedValue(WORKFLOW);
  mocks.startSourceSync.mockReset().mockResolvedValue({ workflowId: "w1", runId: "run-9" });
});
afterEach(() => cleanup());

async function ready() {
  render(<SourcesTab kbId="kb1" kbName="Parts" />);
  return (await screen.findByText("TI parts")).closest("li") as HTMLElement;
}

describe("SourcesTab", () => {
  it("lists name, provider, enabled, last successful sync and the checkpoint summary", async () => {
    const row = await ready();
    expect(within(row).getByText("Acme Parts")).toBeInTheDocument();
    expect(within(row).getByText("Enabled")).toBeInTheDocument();
    expect(row.textContent).toMatch(/Last successful sync/);
    expect(row.textContent).toMatch(/3 segments/);
    expect(row.textContent).toMatch(/cursor stored/);
  });

  it("shows an empty state that points at connections", async () => {
    mocks.listSources.mockResolvedValue([]);
    render(<SourcesTab kbId="kb1" kbName="Parts" />);
    expect(await screen.findByText(/No sources yet/)).toBeInTheDocument();
  });

  it("adds a source with a connection, name and policy options", async () => {
    render(<SourcesTab kbId="kb1" kbName="Parts" />);
    await screen.findByText("TI parts");
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));
    const sheet = await screen.findByRole("dialog");
    await waitFor(() => expect(within(sheet).getByLabelText("Connection")).toHaveTextContent("ti-prod"));
    fireEvent.change(within(sheet).getByLabelText("Connection"), { target: { value: "c1" } });
    fireEvent.change(within(sheet).getByLabelText("Name"), { target: { value: "New" } });
    fireEvent.change(within(sheet).getByLabelText("Page size"), { target: { value: "50" } });
    fireEvent.click(within(sheet).getByLabelText("Include extended data (parametrics, quality)"));
    fireEvent.click(within(sheet).getByRole("button", { name: "Add source" }));
    await waitFor(() => expect(mocks.createSource).toHaveBeenCalled());
    expect(mocks.createSource.mock.calls[0]).toEqual([
      "kb1",
      {
        connection_id: "c1",
        name: "New",
        kind: "product_catalog",
        provider: "acme",
        config: { page_size: 50, include_extended: true, include_inactive: false, document_policy: { types: ["datasheet"] } },
        enabled: true,
      },
    ]);
  });

  describe("repository sources", () => {
    const GH = { ...CONNECTION, id: "c2", name: "repos", kind: "api_token", base_url: "https://api.github.com", config: { provider: "github" } };
    const REPO_SOURCE = { ...SOURCE, id: "s3", kind: "repository_docs", provider: "github", name: "TI docs", connection_id: "c2", config: { repository: "o/r" } };

    async function openSheet() {
      mocks.listConnections.mockResolvedValue([CONNECTION, GH]);
      render(<SourcesTab kbId="kb1" kbName="Docs" />);
      await screen.findByText("TI parts");
      fireEvent.click(screen.getByRole("button", { name: "Add source" }));
      const sheet = await screen.findByRole("dialog");
      await waitFor(() => expect(within(sheet).getByLabelText("Connection")).toHaveTextContent("repos"));
      fireEvent.change(within(sheet).getByLabelText("Connection"), { target: { value: "c2" } });
      fireEvent.change(within(sheet).getByLabelText("Name"), { target: { value: "TI docs" } });
      return sheet;
    }

    it("adds a repository source: repository, ref, include, exclude, map and the delete-missing toggle", async () => {
      const sheet = await openSheet();
      expect(within(sheet).queryByLabelText("Page size")).toBeNull();
      const add = within(sheet).getByRole("button", { name: "Add source" });
      expect(add).toBeDisabled();
      fireEvent.change(within(sheet).getByLabelText("Repository"), { target: { value: "TexasInstruments/mcupsdk-core" } });
      expect(add).toBeDisabled(); // include is required
      fireEvent.change(within(sheet).getByLabelText("Include"), { target: { value: "docs/**\nsource/**/*.md" } });
      fireEvent.change(within(sheet).getByLabelText("Exclude"), { target: { value: "docs/old/**" } });
      fireEvent.change(within(sheet).getByLabelText("Ref"), { target: { value: "main" } });
      fireEvent.change(within(sheet).getByLabelText("Metadata map (YAML)"), { target: { value: "docs/am263x/**: {sdk_family: mcu-plus-sdk, device: AM263x}\n\"**\": {sdk_family: mcu-plus-sdk}" } });
      fireEvent.click(within(sheet).getByLabelText(/Delete documents whose files disappeared/));
      expect(add).toBeEnabled();
      fireEvent.click(add);
      await waitFor(() => expect(mocks.createSource).toHaveBeenCalled());
      expect(mocks.createSource.mock.calls[0]).toEqual([
        "kb1",
        {
          connection_id: "c2", name: "TI docs", kind: "repository_docs", provider: "github",
          config: {
            repository: "TexasInstruments/mcupsdk-core", ref: "main", include: ["docs/**", "source/**/*.md"], exclude: ["docs/old/**"],
            map: [
              { match: "docs/am263x/**", metadata: { sdk_family: "mcu-plus-sdk", device: "AM263x" } },
              { match: "**", metadata: { sdk_family: "mcu-plus-sdk" } },
            ],
            document_policy: { delete_missing: true },
          },
          enabled: true,
        },
      ]);
    });

    it("validates the repository name and the map before it can be added", async () => {
      const sheet = await openSheet();
      const add = within(sheet).getByRole("button", { name: "Add source" });
      fireEvent.change(within(sheet).getByLabelText("Repository"), { target: { value: "just-a-name" } });
      expect(within(sheet).getByText("Use owner/name.")).toBeInTheDocument();
      fireEvent.change(within(sheet).getByLabelText("Repository"), { target: { value: "o/r" } });
      fireEvent.change(within(sheet).getByLabelText("Include"), { target: { value: "**" } });
      expect(add).toBeEnabled();
      fireEvent.change(within(sheet).getByLabelText("Metadata map (YAML)"), { target: { value: "a: {path: x}" } });
      expect(add).toBeDisabled();
      expect(within(sheet).getByRole("alert")).toHaveTextContent(/reserved/);
      fireEvent.change(within(sheet).getByLabelText("Metadata map (YAML)"), { target: { value: "a: [" } });
      expect(within(sheet).getByRole("alert")).toHaveTextContent(/not valid YAML/);
      fireEvent.change(within(sheet).getByLabelText("Metadata map (YAML)"), { target: { value: "" } });
      expect(add).toBeEnabled();
      expect(mocks.createSource).not.toHaveBeenCalled();
    });

    it("has no axe violations in the repository form", async () => {
      const sheet = await openSheet();
      fireEvent.change(within(sheet).getByLabelText("Metadata map (YAML)"), { target: { value: "a: [" } });
      await expectNoAxeViolations(document.body);
    }, AXE_TIMEOUT_MS);

    it("runs a targeted sync of file paths", async () => {
      mocks.listSources.mockResolvedValue([REPO_SOURCE]);
      render(<SourcesTab kbId="kb1" kbName="Docs" />);
      const row = (await screen.findByText("TI docs")).closest("li") as HTMLElement;
      expect(within(row).getByText("GitHub")).toBeInTheDocument();
      fireEvent.click(within(row).getByRole("button", { name: "Run sync for TI docs" }));
      const sheet = await screen.findByRole("dialog");
      expect(within(sheet).getByRole("option", { name: "Full repository" })).toBeInTheDocument();
      fireEvent.change(within(sheet).getByLabelText("File paths"), { target: { value: "docs/a.md\ndocs/b.rst" } });
      expect(within(sheet).getByText("2 of 100 file paths")).toBeInTheDocument();
      fireEvent.click(within(sheet).getByRole("button", { name: "Start sync" }));
      await waitFor(() => expect(mocks.startSourceSync).toHaveBeenCalledWith("s3", "targeted", { partNumbers: ["docs/a.md", "docs/b.rst"], restart: false }));
    });
  });

  it("starts a targeted sync through the workflow and links to the run", async () => {
    const row = await ready();
    fireEvent.click(within(row).getByRole("button", { name: "Run sync for TI parts" }));
    const sheet = await screen.findByRole("dialog");
    const start = within(sheet).getByRole("button", { name: "Start sync" });
    expect(start).toBeDisabled(); // targeted needs part numbers
    fireEvent.change(within(sheet).getByLabelText("Part numbers"), { target: { value: "A1, B2\nA1" } });
    expect(within(sheet).getByText("2 of 100 part numbers")).toBeInTheDocument();
    fireEvent.click(start);
    await waitFor(() => expect(mocks.startSourceSync).toHaveBeenCalledWith("s1", "targeted", { partNumbers: ["A1", "B2"], restart: false }));
    const link = await screen.findByRole("link", { name: "View workflow run" });
    expect(link).toHaveAttribute("href", "/workflows/w1/runs/run-9");
  });

  it("refuses more than 100 part numbers", async () => {
    const row = await ready();
    fireEvent.click(within(row).getByRole("button", { name: "Run sync for TI parts" }));
    const sheet = await screen.findByRole("dialog");
    fireEvent.change(within(sheet).getByLabelText("Part numbers"), { target: { value: Array.from({ length: 101 }, (_, i) => `P${i}`).join("\n") } });
    expect(within(sheet).getByRole("button", { name: "Start sync" })).toBeDisabled();
    expect(within(sheet).getByText(/at most 100/i)).toBeInTheDocument();
  });

  it("incremental needs no part numbers; full offers a restart toggle", async () => {
    const row = await ready();
    fireEvent.click(within(row).getByRole("button", { name: "Run sync for TI parts" }));
    const sheet = await screen.findByRole("dialog");
    fireEvent.change(within(sheet).getByLabelText("Mode"), { target: { value: "incremental" } });
    expect(within(sheet).queryByLabelText("Part numbers")).toBeNull();
    expect(within(sheet).queryByLabelText(/Restart/)).toBeNull();
    fireEvent.change(within(sheet).getByLabelText("Mode"), { target: { value: "full" } });
    fireEvent.click(within(sheet).getByLabelText(/Restart from the beginning/));
    fireEvent.click(within(sheet).getByRole("button", { name: "Start sync" }));
    await waitFor(() => expect(mocks.startSourceSync).toHaveBeenCalledWith("s1", "full", { partNumbers: [], restart: true }));
  });

  it("shows a stable error when the run could not start", async () => {
    mocks.startSourceSync.mockRejectedValue(new KbSourceError(404, "sync_workflow_missing", "The KB Source Synchronization workflow is not published yet."));
    const row = await ready();
    fireEvent.click(within(row).getByRole("button", { name: "Run sync for TI parts" }));
    const sheet = await screen.findByRole("dialog");
    fireEvent.change(within(sheet).getByLabelText("Mode"), { target: { value: "incremental" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Start sync" }));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent("not published yet");
  });

  it("offers to publish the sync workflow when none is published, and disables Run sync until then", async () => {
    mocks.findSyncWorkflow.mockResolvedValue(null);
    const row = await ready();
    expect(within(row).getByRole("button", { name: "Run sync for TI parts" })).toBeDisabled();
    fireEvent.click(await screen.findByRole("button", { name: "Publish the sync workflow" }));
    await waitFor(() => expect(mocks.publishSyncWorkflow).toHaveBeenCalled());
    await waitFor(() => expect(within(row).getByRole("button", { name: "Run sync for TI parts" })).toBeEnabled());
    expect(screen.queryByRole("button", { name: "Publish the sync workflow" })).toBeNull();
  });

  it("lists the syncs with status, the report summary, stop reason and a run link", async () => {
    const row = await ready();
    fireEvent.click(within(row).getByRole("button", { name: "Show syncs for TI parts" }));
    const item = (await within(row).findByText("completed")).closest("li") as HTMLElement;
    await waitFor(() => expect(item.textContent).toMatch(/discovered 5/));
    for (const text of [/inserted 2/, /updated 1/, /unchanged 2/, /documents queued 3/, /failed 0/, /stopped: budget/]) expect(item.textContent).toMatch(text);
    expect(within(item).getByRole("link", { name: /Run/ })).toHaveAttribute("href", "/workflows/w1/runs/r-parent");
  });

  it("disables a source", async () => {
    const row = await ready();
    fireEvent.click(within(row).getByRole("button", { name: "Disable TI parts" }));
    await waitFor(() => expect(mocks.updateSource).toHaveBeenCalledWith("kb1", "s1", { enabled: false }));
  });

  it("has no axe violations, with the run sheet open too", async () => {
    const row = await ready();
    fireEvent.click(within(row).getByRole("button", { name: "Run sync for TI parts" }));
    await screen.findByRole("dialog");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
