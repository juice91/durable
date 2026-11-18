import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listConnections: vi.fn(),
  createConnection: vi.fn(),
  updateConnection: vi.fn(),
  deleteConnection: vi.fn(),
  checkConnection: vi.fn(),
  sourcesUsingConnection: vi.fn(),
  listSecrets: vi.fn(),
  putSecret: vi.fn(),
}));
vi.mock("@/lib/kbSources", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  listConnections: mocks.listConnections,
  createConnection: mocks.createConnection,
  updateConnection: mocks.updateConnection,
  deleteConnection: mocks.deleteConnection,
  checkConnection: mocks.checkConnection,
  sourcesUsingConnection: mocks.sourcesUsingConnection,
}));
vi.mock("@/lib/secrets", async (importOriginal) => ({ ...(await importOriginal<object>()), listSecrets: mocks.listSecrets, putSecret: mocks.putSecret }));

import { ConnectionsSection } from "./ConnectionsSection";
import { KbSourceError } from "@/lib/kbSources";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";

const FAKE = "fake-client-secret-value-0001";
const T = "2026-11-18T10:00:00Z";
const CONNECTION = {
  id: "c1",
  name: "ti-prod",
  kind: "oauth2_client_credentials",
  base_url: "https://api.vendor.example",
  config: { provider: "acme_catalog", token_url: "https://api.vendor.example/v1/oauth/accesstoken" },
  credential_refs: { client_id: "secret:conn-id", client_secret: "secret:conn-secret" },
  enabled: true,
  last_checked_at: T,
  last_status: "ok",
  last_error_code: null,
  created_at: T,
  updated_at: T,
};

beforeEach(() => {
  mocks.listConnections.mockReset().mockResolvedValue([CONNECTION]);
  mocks.createConnection.mockReset().mockResolvedValue({ ...CONNECTION, id: "c2", name: "ti-new" });
  mocks.updateConnection.mockReset().mockResolvedValue({ ...CONNECTION, enabled: false });
  mocks.deleteConnection.mockReset().mockResolvedValue(undefined);
  mocks.checkConnection.mockReset().mockResolvedValue({ ok: true, checked_at: T, error_code: null });
  mocks.sourcesUsingConnection.mockReset().mockResolvedValue([]);
  mocks.listSecrets.mockReset().mockResolvedValue([
    { name: "conn-id", created_at: T, updated_at: T },
    { name: "conn-secret", created_at: T, updated_at: T },
  ]);
  mocks.putSecret.mockReset().mockResolvedValue({ restarted: [] });
});
afterEach(() => cleanup());

async function openAdd() {
  render(<ConnectionsSection />);
  await screen.findByText("ti-prod");
  fireEvent.click(screen.getByRole("button", { name: "Add connection" }));
  return await screen.findByRole("dialog");
}

describe("ConnectionsSection", () => {
  it("lists name, provider, status chip and when it was checked, in a labelled region", async () => {
    render(<ConnectionsSection />);
    const row = (await screen.findByText("ti-prod")).closest("li") as HTMLElement;
    expect(screen.getByRole("region", { name: "Connections" })).toBeInTheDocument();
    expect(within(row).getByText("Acme Semiconductor")).toBeInTheDocument();
    expect(within(row).getByText("Ok")).toBeInTheDocument();
    expect(row.textContent).toMatch(/Checked/);
  });

  it("shows a never-checked connection and an empty state", async () => {
    mocks.listConnections.mockResolvedValue([{ ...CONNECTION, last_status: null, last_checked_at: null }]);
    render(<ConnectionsSection />);
    expect(await screen.findByText("Not checked")).toBeInTheDocument();
    cleanup();
    mocks.listConnections.mockResolvedValue([]);
    render(<ConnectionsSection />);
    expect(await screen.findByText(/No connections yet/)).toBeInTheDocument();
  });

  it("shows the stored error code text on a failed connection", async () => {
    mocks.listConnections.mockResolvedValue([{ ...CONNECTION, last_status: "error", last_error_code: "connection_auth_failed" }]);
    render(<ConnectionsSection />);
    expect(await screen.findByText("Error")).toBeInTheDocument();
    expect(screen.getByText(/The provider rejected the credentials\./)).toBeInTheDocument();
  });

  it("adds a connection with secret references chosen from the stored names", async () => {
    const sheet = await openAdd();
    expect(within(sheet).getByLabelText("Base URL")).toHaveValue("https://api.vendor.example");
    expect(within(sheet).getByLabelText("Token URL")).toHaveValue("https://api.vendor.example/v1/oauth/accesstoken");
    fireEvent.change(within(sheet).getByLabelText("Name"), { target: { value: "ti-new" } });
    await waitFor(() => expect(within(sheet).getByLabelText("Client ID")).toHaveTextContent("conn-id"));
    fireEvent.change(within(sheet).getByLabelText("Client ID"), { target: { value: "conn-id" } });
    fireEvent.change(within(sheet).getByLabelText("Client secret"), { target: { value: "conn-secret" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Add connection" }));
    await waitFor(() => expect(mocks.createConnection).toHaveBeenCalled());
    expect(mocks.createConnection.mock.calls[0][0]).toEqual({
      name: "ti-new",
      kind: "oauth2_client_credentials",
      base_url: "https://api.vendor.example",
      config: { provider: "acme_catalog", token_url: "https://api.vendor.example/v1/oauth/accesstoken" },
      credential_refs: { client_id: "secret:conn-id", client_secret: "secret:conn-secret" },
      enabled: true,
    });
    await waitFor(() => expect(mocks.listConnections).toHaveBeenCalledTimes(2));
  });

  it("adds an API-token connection: one optional Token picker, no OAuth fields, no value", async () => {
    const sheet = await openAdd();
    fireEvent.change(within(sheet).getByLabelText("Provider"), { target: { value: "github" } });
    expect(within(sheet).getByLabelText("Base URL")).toHaveValue("https://api.github.com");
    expect(within(sheet).queryByLabelText("Token URL")).toBeNull();
    expect(within(sheet).queryByLabelText("Client ID")).toBeNull();
    expect(within(sheet).queryByLabelText("Client secret")).toBeNull();
    expect(within(sheet).getAllByRole("combobox").map((el) => el.id).length).toBe(2); // provider + Token
    fireEvent.change(within(sheet).getByLabelText("Name"), { target: { value: "repos" } });
    // the token is optional: a public repository needs none
    expect(within(sheet).getByRole("button", { name: "Add connection" })).toBeEnabled();
    await waitFor(() => expect(within(sheet).getByLabelText("Token")).toHaveTextContent("conn-id"));
    fireEvent.change(within(sheet).getByLabelText("Token"), { target: { value: "conn-secret" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Add connection" }));
    await waitFor(() => expect(mocks.createConnection).toHaveBeenCalled());
    expect(mocks.createConnection.mock.calls[0][0]).toEqual({
      name: "repos", kind: "api_token", base_url: "https://api.github.com", config: { provider: "github" },
      credential_refs: { token: "secret:conn-secret" }, enabled: true,
    });
  });

  it("adds an API-token connection without a token as an empty reference map", async () => {
    const sheet = await openAdd();
    fireEvent.change(within(sheet).getByLabelText("Provider"), { target: { value: "github" } });
    fireEvent.change(within(sheet).getByLabelText("Name"), { target: { value: "public" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Add connection" }));
    await waitFor(() => expect(mocks.createConnection).toHaveBeenCalled());
    expect(mocks.createConnection.mock.calls[0][0].credential_refs).toEqual({});
  });

  it("edits an API-token connection with its token preselected and the provider fixed", async () => {
    mocks.listConnections.mockResolvedValue([{ ...CONNECTION, id: "c9", name: "repos", kind: "api_token", base_url: "https://api.github.com", config: { provider: "github" }, credential_refs: { token: "secret:conn-secret" } }]);
    render(<ConnectionsSection />);
    await screen.findByText("repos");
    expect(screen.getByText("GitHub")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit repos" }));
    const sheet = await screen.findByRole("dialog");
    expect(within(sheet).getByLabelText("Provider")).toBeDisabled();
    await waitFor(() => expect(within(sheet).getByLabelText("Token")).toHaveValue("conn-secret"));
    fireEvent.change(within(sheet).getByLabelText("Token"), { target: { value: "" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(mocks.updateConnection).toHaveBeenCalled());
    expect(mocks.updateConnection.mock.calls[0][1]).toMatchObject({ credential_refs: {}, config: { provider: "github" } });
  });

  it("keeps Add disabled until the name and both credentials are chosen", async () => {
    const sheet = await openAdd();
    expect(within(sheet).getByRole("button", { name: "Add connection" })).toBeDisabled();
  });

  it("creates a secret inline with a masked input, selects it and never shows the value", async () => {
    const sheet = await openAdd();
    fireEvent.change(within(sheet).getByLabelText("Client ID"), { target: { value: "__new__" } });
    const name = within(sheet).getByLabelText("New secret name");
    const value = within(sheet).getByLabelText("New secret value");
    expect(value).toHaveAttribute("type", "password");
    expect(value).toHaveAttribute("autocomplete", "off");
    fireEvent.change(name, { target: { value: "conn-client-id" } });
    fireEvent.change(value, { target: { value: FAKE } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Save secret" }));
    await waitFor(() => expect(mocks.putSecret).toHaveBeenCalledWith("conn-client-id", FAKE));
    await waitFor(() => expect(within(sheet).getByLabelText("Client ID")).toHaveValue("conn-client-id"));
    expect(within(sheet).queryByLabelText("New secret value")).toBeNull();
    expect(document.body.textContent).not.toContain(FAKE);
  });

  it("shows an inline secret error without echoing the typed value", async () => {
    mocks.putSecret.mockRejectedValue(new Error("Secret names use lowercase letters, digits and dashes, starting with a letter."));
    const sheet = await openAdd();
    fireEvent.change(within(sheet).getByLabelText("Client ID"), { target: { value: "__new__" } });
    fireEvent.change(within(sheet).getByLabelText("New secret name"), { target: { value: "Bad" } });
    fireEvent.change(within(sheet).getByLabelText("New secret value"), { target: { value: FAKE } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Save secret" }));
    expect(await within(sheet).findByText(/lowercase letters/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(FAKE);
  });

  it("edits a connection with its references preselected, and patches", async () => {
    render(<ConnectionsSection />);
    await screen.findByText("ti-prod");
    fireEvent.click(screen.getByRole("button", { name: "Edit ti-prod" }));
    const sheet = await screen.findByRole("dialog");
    await waitFor(() => expect(within(sheet).getByLabelText("Client ID")).toHaveValue("conn-id"));
    expect(within(sheet).getByLabelText("Client secret")).toHaveValue("conn-secret");
    fireEvent.change(within(sheet).getByLabelText("Name"), { target: { value: "conn-renamed" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(mocks.updateConnection).toHaveBeenCalled());
    const [id, body] = mocks.updateConnection.mock.calls[0];
    expect(id).toBe("c1");
    expect(body).toMatchObject({ name: "conn-renamed", credential_refs: { client_id: "secret:conn-id", client_secret: "secret:conn-secret" } });
  });

  it("checks a connection and shows ok or the stable code text", async () => {
    render(<ConnectionsSection />);
    await screen.findByText("ti-prod");
    fireEvent.click(screen.getByRole("button", { name: "Check ti-prod" }));
    await waitFor(() => expect(mocks.checkConnection).toHaveBeenCalledWith("c1"));
    expect(await screen.findByTestId("connection-check")).toHaveTextContent("Connection ok");
    mocks.checkConnection.mockResolvedValue({ ok: false, checked_at: T, error_code: "secret_missing" });
    fireEvent.click(screen.getByRole("button", { name: "Check ti-prod" }));
    await waitFor(() => expect(screen.getByTestId("connection-check")).toHaveTextContent("A stored secret the connection refers to is missing."));
  });

  it("disables and re-enables a connection", async () => {
    render(<ConnectionsSection />);
    await screen.findByText("ti-prod");
    fireEvent.click(screen.getByRole("button", { name: "Disable ti-prod" }));
    await waitFor(() => expect(mocks.updateConnection).toHaveBeenCalledWith("c1", { enabled: false }));
  });

  it("deletes after a confirm", async () => {
    render(<ConnectionsSection />);
    await screen.findByText("ti-prod");
    fireEvent.click(screen.getByRole("button", { name: "Delete ti-prod" }));
    const dialog = await screen.findByRole("dialog");
    expect(mocks.deleteConnection).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(mocks.deleteConnection).toHaveBeenCalledWith("c1"));
  });

  it("names the sources when the delete is refused as in use", async () => {
    mocks.deleteConnection.mockRejectedValue(new KbSourceError(409, "connection_in_use", "A source still uses this connection."));
    mocks.sourcesUsingConnection.mockResolvedValue([{ id: "s1", name: "TI parts", knowledgeBaseId: "kb1", knowledgeBaseName: "Parts" }]);
    render(<ConnectionsSection />);
    await screen.findByText("ti-prod");
    fireEvent.click(screen.getByRole("button", { name: "Delete ti-prod" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    const alert = await within(dialog).findByRole("alert");
    await waitFor(() => expect(alert).toHaveTextContent("TI parts"));
    expect(alert).toHaveTextContent("Parts");
  });

  it("has no axe violations, with the add sheet open too", async () => {
    const sheet = await openAdd();
    await waitFor(() => expect(within(sheet).getByLabelText("Client ID")).toHaveTextContent("conn-id"));
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
