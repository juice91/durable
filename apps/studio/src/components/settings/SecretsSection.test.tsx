import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ listSecrets: vi.fn(), putSecret: vi.fn(), deleteSecret: vi.fn() }));
vi.mock("@/lib/secrets", async (importOriginal) => ({ ...(await importOriginal<object>()), ...mocks }));

import { SecretsSection } from "./SecretsSection";
import { ApiError } from "@/lib/api";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";

const FAKE = "fake-secret-value-000";
beforeEach(() => {
  mocks.listSecrets.mockReset().mockResolvedValue([{ name: "slack-token", created_at: "2026-11-17T00:00:00Z", updated_at: "2026-11-17T00:00:00Z" }]);
  mocks.putSecret.mockReset().mockResolvedValue({ restarted: [] });
  mocks.deleteSecret.mockReset().mockResolvedValue(undefined);
});
afterEach(() => cleanup());

describe("SecretsSection", () => {
  it("lists names only, in a labelled region", async () => {
    render(<SecretsSection />);
    expect(await screen.findByText("slack-token")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Secrets" })).toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    mocks.listSecrets.mockResolvedValue([]);
    render(<SecretsSection />);
    expect(await screen.findByText(/No secrets yet/)).toBeInTheDocument();
  });

  it("adds a secret with a masked value, then clears the field and reloads", async () => {
    render(<SecretsSection />);
    await screen.findByText("slack-token");
    fireEvent.change(screen.getByLabelText("Secret name"), { target: { value: "github-token" } });
    const value = screen.getByLabelText("Secret value");
    expect(value).toHaveAttribute("type", "password");
    expect(value).toHaveAttribute("autocomplete", "off");
    fireEvent.change(value, { target: { value: FAKE } });
    fireEvent.click(screen.getByRole("button", { name: "Add secret" }));
    await waitFor(() => expect(mocks.putSecret).toHaveBeenCalledWith("github-token", FAKE));
    await waitFor(() => expect(screen.getByLabelText("Secret value")).toHaveValue(""));
    expect(mocks.listSecrets).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).not.toContain(FAKE);
  });

  it("disables Add until name and value are given", async () => {
    render(<SecretsSection />);
    await screen.findByText("slack-token");
    expect(screen.getByRole("button", { name: "Add secret" })).toBeDisabled();
  });

  it("shows the error and keeps the form on a failed add", async () => {
    mocks.putSecret.mockRejectedValue(new Error("Secret names are lowercase letters, digits and dashes."));
    render(<SecretsSection />);
    await screen.findByText("slack-token");
    fireEvent.change(screen.getByLabelText("Secret name"), { target: { value: "Bad" } });
    fireEvent.change(screen.getByLabelText("Secret value"), { target: { value: FAKE } });
    fireEvent.click(screen.getByRole("button", { name: "Add secret" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("lowercase");
    expect(document.body.textContent).not.toContain(FAKE);
  });

  it("deletes after a confirm", async () => {
    render(<SecretsSection />);
    await screen.findByText("slack-token");
    fireEvent.click(screen.getByRole("button", { name: "Delete slack-token" }));
    const dialog = await screen.findByRole("dialog");
    expect(mocks.deleteSecret).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(mocks.deleteSecret).toHaveBeenCalledWith("slack-token"));
  });

  it("shows Used by with links to the servers and disables Delete while in use", async () => {
    mocks.listSecrets.mockResolvedValue([
      { name: "slack-token", created_at: "a", updated_at: "b", used_by: [{ server_id: "srv-1", name: "files" }] },
      { name: "free-token", created_at: "a", updated_at: "b", used_by: [] },
    ]);
    render(<SecretsSection />);
    const link = await screen.findByRole("link", { name: "files" });
    expect(link).toHaveAttribute("href", "/mcp/srv-1");
    expect(screen.getByRole("button", { name: "Delete slack-token" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete free-token" })).toBeEnabled();
  });

  it("handles a 409 secret_in_use on delete: closes the dialog, names the servers, reloads", async () => {
    mocks.deleteSecret.mockRejectedValue(new ApiError(409, "In use by files."));
    render(<SecretsSection />);
    await screen.findByText("slack-token");
    fireEvent.click(screen.getByRole("button", { name: "Delete slack-token" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByTestId("secrets-status")).toHaveTextContent("In use by files.");
    expect(mocks.listSecrets).toHaveBeenCalledTimes(2);
  });

  it("reports the servers restarted by an update", async () => {
    mocks.putSecret.mockResolvedValue({ restarted: ["srv-1", "srv-2"] });
    render(<SecretsSection />);
    await screen.findByText("slack-token");
    fireEvent.change(screen.getByLabelText("Secret name"), { target: { value: "slack-token" } });
    fireEvent.change(screen.getByLabelText("Secret value"), { target: { value: FAKE } });
    fireEvent.click(screen.getByRole("button", { name: "Add secret" }));
    await waitFor(() => expect(screen.getByTestId("secrets-status")).toHaveTextContent(/Restarted 2 servers/));
  });

  it("has no axe violations, with the delete dialog open too", async () => {
    render(<SecretsSection />);
    await screen.findByText("slack-token");
    await expectNoAxeViolations(document.body);
    fireEvent.click(screen.getByRole("button", { name: "Delete slack-token" }));
    await screen.findByRole("dialog");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
