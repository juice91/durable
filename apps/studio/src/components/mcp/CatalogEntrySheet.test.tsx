import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CatalogEntrySheet } from "./CatalogEntrySheet";
import { LOCAL_ENTRY, REMOTE_ENTRY } from "./catalogFixtures";
import { mcpCatalog } from "@/lib/mcpCatalog";
import { ApiError } from "@/lib/api";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";

vi.mock("@/lib/mcpCatalog", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpCatalog")>();
  return { ...actual, mcpCatalog: { ...actual.mcpCatalog, install: vi.fn() } };
});

const created = { id: "srv-9", name: "files" } as never;

describe("CatalogEntrySheet", () => {
  beforeEach(() => {
    vi.mocked(mcpCatalog.install).mockReset();
  });

  it("shows packages, remotes and env vars with required and secret markers", () => {
    render(<CatalogEntrySheet entry={LOCAL_ENTRY} onClose={vi.fn()} onInstalled={vi.fn()} />);
    expect(screen.getByRole("radio", { name: /@example\/files/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /example-files/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /files\.example\.test/ })).toBeInTheDocument();
    const secret = screen.getByLabelText(/API_TOKEN/);
    expect(secret).toHaveAttribute("type", "password");
    expect(secret).toBeRequired();
    expect(screen.getByLabelText(/ROOT_DIR/)).toHaveAttribute("type", "text");
    expect(screen.getByLabelText(/LOG_LEVEL/)).not.toBeRequired();
    expect(screen.getAllByText("secret").length).toBeGreaterThan(0);
  });

  it("posts the install with package index, env and name, then reports the server", async () => {
    vi.mocked(mcpCatalog.install).mockResolvedValue(created);
    const onInstalled = vi.fn();
    render(<CatalogEntrySheet entry={LOCAL_ENTRY} onClose={vi.fn()} onInstalled={onInstalled} />);
    fireEvent.change(screen.getByLabelText(/ROOT_DIR/), { target: { value: "/data" } });
    fireEvent.change(screen.getByLabelText(/API_TOKEN/), { target: { value: "tok" } });
    fireEvent.change(screen.getByLabelText("Server name"), { target: { value: "files" } });
    fireEvent.click(screen.getByRole("button", { name: "Run server" }));
    await waitFor(() => expect(onInstalled).toHaveBeenCalledWith(created));
    expect(mcpCatalog.install).toHaveBeenCalledWith({
      catalog_name: "io.example/files",
      package_index: 0,
      name: "files",
      env: { ROOT_DIR: "/data", API_TOKEN: "tok" },
    });
  });

  it("blocks submit until required env is filled", () => {
    render(<CatalogEntrySheet entry={LOCAL_ENTRY} onClose={vi.fn()} onInstalled={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Run server" }));
    expect(mcpCatalog.install).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/ROOT_DIR/);
  });

  it("registers a remote with remote_index and no env", async () => {
    vi.mocked(mcpCatalog.install).mockResolvedValue(created);
    render(<CatalogEntrySheet entry={REMOTE_ENTRY} onClose={vi.fn()} onInstalled={vi.fn()} />);
    expect(screen.queryByLabelText(/ROOT_DIR/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    await waitFor(() => expect(mcpCatalog.install).toHaveBeenCalledWith({ catalog_name: "io.example/search", remote_index: 0 }));
  });

  it("switching to a remote sends remote_index", async () => {
    vi.mocked(mcpCatalog.install).mockResolvedValue(created);
    render(<CatalogEntrySheet entry={LOCAL_ENTRY} onClose={vi.fn()} onInstalled={vi.fn()} />);
    fireEvent.click(screen.getByRole("radio", { name: /files\.example\.test/ }));
    expect(screen.queryByLabelText(/ROOT_DIR/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    await waitFor(() => expect(mcpCatalog.install).toHaveBeenCalledWith({ catalog_name: "io.example/files", remote_index: 0 }));
  });

  async function failWith(error: ApiError) {
    vi.mocked(mcpCatalog.install).mockImplementation(() => Promise.reject(error));
    render(<CatalogEntrySheet entry={REMOTE_ENTRY} onClose={vi.fn()} onInstalled={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    const alert = await screen.findByRole("alert");
    return alert.textContent ?? "";
  }

  it("shows a readable 409 error", async () => {
    expect(await failWith(new ApiError(409, "name taken"))).toMatch(/already uses that name/i);
  });

  it("shows the 422 reason", async () => {
    expect(await failWith(new ApiError(422, "undeclared env: X"))).toMatch(/undeclared env: X/);
  });

  it("has no axe violations", async () => {
    render(<CatalogEntrySheet entry={LOCAL_ENTRY} onClose={vi.fn()} onInstalled={vi.fn()} />);
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
