import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ listSecrets: vi.fn(), putSecret: vi.fn(), deleteSecret: vi.fn() }));
vi.mock("@/lib/secrets", async (importOriginal) => ({ ...(await importOriginal<object>()), ...mocks }));

import { SecretsSection } from "./SecretsSection";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";

const FAKE = "fake-secret-value-000";
beforeEach(() => {
  mocks.listSecrets.mockReset().mockResolvedValue([{ name: "slack-token", created_at: "2026-11-17T00:00:00Z", updated_at: "2026-11-17T00:00:00Z" }]);
  mocks.putSecret.mockReset().mockResolvedValue(undefined);
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

  it("has no axe violations, with the delete dialog open too", async () => {
    render(<SecretsSection />);
    await screen.findByText("slack-token");
    await expectNoAxeViolations(document.body);
    fireEvent.click(screen.getByRole("button", { name: "Delete slack-token" }));
    await screen.findByRole("dialog");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
