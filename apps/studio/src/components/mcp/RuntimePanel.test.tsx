import { act as rtlAct, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RuntimePanel } from "./RuntimePanel";
import { mcpServers, type McpRuntime, type McpRuntimeState } from "@/lib/mcpServers";
import { ApiError } from "@/lib/api";
import * as secrets from "@/lib/secrets";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";

vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, runtime: vi.fn(), runtimeAction: vi.fn(), runtimeLogs: vi.fn(), update: vi.fn() } };
});

vi.mock("@/lib/secrets", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/secrets")>();
  return { ...actual, listSecrets: vi.fn() };
});

const runtime = (state: McpRuntimeState, extra: Partial<McpRuntime> = {}): McpRuntime => ({
  state, message: null, reachable: state === "running", last_error: null, last_used_at: null, updated_at: null,
  image: "registry.test/files:1", catalog_name: "io.example/files", package: { registry_type: "npm", identifier: "@example/files", version: "1.2.0", transport: "stdio" }, ...extra,
});

const btn = (name: string) => screen.getByRole("button", { name });

describe("RuntimePanel", () => {
  beforeEach(() => {
    vi.mocked(mcpServers.runtime).mockReset().mockResolvedValue(runtime("running", { message: "Pod ready" }));
    vi.mocked(mcpServers.runtimeAction).mockReset();
    vi.mocked(mcpServers.update).mockReset().mockResolvedValue({} as never);
    vi.mocked(secrets.listSecrets).mockReset().mockResolvedValue([
      { name: "slack-token", created_at: "a", updated_at: "b", used_by: [] },
      { name: "other-token", created_at: "a", updated_at: "b", used_by: [] },
    ]);
    vi.mocked(mcpServers.runtimeLogs).mockReset().mockResolvedValue({ lines: ["line one", "line two"], tail: 200 });
  });

  it("shows state, message and logs", async () => {
    render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    expect(await screen.findByTestId("runtime-state")).toHaveTextContent("Running");
    expect(screen.getByText("Pod ready")).toBeInTheDocument();
    expect(await screen.findByText(/line one/)).toBeInTheDocument();
    expect(mcpServers.runtimeLogs).toHaveBeenCalledWith("s1", 200);
  });

  it("lists env names, showing refs as secret:<name> and never a value", async () => {
    vi.mocked(mcpServers.runtime).mockResolvedValue(runtime("running", { env: [{ name: "API_TOKEN", ref: "secret:slack-token" }, { name: "ROOT_DIR", ref: null }] }));
    render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    const list = await screen.findByRole("list", { name: "Environment" });
    expect(list).toHaveTextContent("API_TOKEN");
    expect(list).toHaveTextContent("secret:slack-token");
    expect(list).toHaveTextContent("ROOT_DIR");
    expect(list).toHaveTextContent("Stored with the server");
  });

  it("changes a variable to another stored secret through the server update, then resyncs", async () => {
    vi.mocked(mcpServers.runtime).mockResolvedValue(runtime("running", { env: [{ name: "API_TOKEN", ref: "secret:slack-token" }] }));
    const onChanged = vi.fn();
    render(<RuntimePanel serverId="s1" enabled onChanged={onChanged} />);
    fireEvent.click(await screen.findByRole("button", { name: "Change API_TOKEN" }));
    fireEvent.change(await screen.findByLabelText("Secret for API_TOKEN"), { target: { value: "other-token" } });
    fireEvent.click(screen.getByRole("button", { name: "Save API_TOKEN" }));
    await waitFor(() => expect(mcpServers.update).toHaveBeenCalledWith("s1", { env: { API_TOKEN: "secret:other-token" } }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(vi.mocked(mcpServers.runtime).mock.calls.length).toBeGreaterThan(1);
  });

  it("shows the update error and keeps the picker open", async () => {
    vi.mocked(mcpServers.runtime).mockResolvedValue(runtime("running", { env: [{ name: "API_TOKEN", ref: null }] }));
    vi.mocked(mcpServers.update).mockRejectedValue(new ApiError(422, "unknown secret"));
    render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Change API_TOKEN" }));
    fireEvent.change(await screen.findByLabelText("Secret for API_TOKEN"), { target: { value: "other-token" } });
    fireEvent.click(screen.getByRole("button", { name: "Save API_TOKEN" }));
    expect(await screen.findByText(/unknown secret/)).toBeInTheDocument();
    expect(screen.getByLabelText("Secret for API_TOKEN")).toBeInTheDocument();
  });

  it.each([
    ["pending", { start: false, stop: true, restart: false }],
    ["starting", { start: false, stop: true, restart: false }],
    ["running", { start: false, stop: true, restart: true }],
    ["idle", { start: false, stop: true, restart: true }],
    ["stopping", { start: false, stop: false, restart: false }],
    ["stopped", { start: true, stop: false, restart: false }],
    ["failed", { start: true, stop: true, restart: true }],
  ] as const)("%s: buttons enabled per state", async (state, enabled) => {
    vi.mocked(mcpServers.runtime).mockResolvedValue(runtime(state));
    render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    await screen.findByTestId("runtime-state");
    for (const [action, label] of [["start", "Start"], ["stop", "Stop"], ["restart", "Restart"]] as const) {
      if (enabled[action]) expect(btn(label)).toBeEnabled();
      else expect(btn(label)).toBeDisabled();
    }
  });

  it("disables start and restart on a disabled server", async () => {
    vi.mocked(mcpServers.runtime).mockResolvedValue(runtime("stopped"));
    render(<RuntimePanel serverId="s1" enabled={false} onChanged={vi.fn()} />);
    await screen.findByTestId("runtime-state");
    expect(btn("Start")).toBeDisabled();
  });

  it("calls the matching action route and shows the new state", async () => {
    vi.mocked(mcpServers.runtimeAction).mockResolvedValue(runtime("stopping"));
    const onChanged = vi.fn();
    render(<RuntimePanel serverId="s1" enabled onChanged={onChanged} />);
    await screen.findByTestId("runtime-state");
    fireEvent.click(btn("Stop"));
    await waitFor(() => expect(mcpServers.runtimeAction).toHaveBeenCalledWith("s1", "stop"));
    await waitFor(() => expect(screen.getByTestId("runtime-state")).toHaveTextContent("Stopping"));
    expect(onChanged).toHaveBeenCalled();
  });

  it("restart calls restart", async () => {
    vi.mocked(mcpServers.runtimeAction).mockResolvedValue(runtime("starting"));
    render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    await screen.findByTestId("runtime-state");
    fireEvent.click(btn("Restart"));
    await waitFor(() => expect(mcpServers.runtimeAction).toHaveBeenCalledWith("s1", "restart"));
  });

  it("shows an action error", async () => {
    vi.mocked(mcpServers.runtimeAction).mockRejectedValue(new ApiError(409, "server is disabled"));
    render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    await screen.findByTestId("runtime-state");
    fireEvent.click(btn("Restart"));
    expect(await screen.findByRole("alert")).toHaveTextContent("server is disabled");
  });

  it("shows the last error", async () => {
    vi.mocked(mcpServers.runtime).mockResolvedValue(runtime("failed", { last_error: "ImagePullBackOff" }));
    render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    expect(await screen.findByText(/ImagePullBackOff/)).toBeInTheDocument();
  });

  it("refreshes logs and honours the tail selector", async () => {
    render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    await screen.findByText(/line one/);
    vi.mocked(mcpServers.runtimeLogs).mockResolvedValue({ lines: ["fresh line"], tail: 500 });
    fireEvent.change(screen.getByLabelText("Lines"), { target: { value: "500" } });
    await waitFor(() => expect(mcpServers.runtimeLogs).toHaveBeenLastCalledWith("s1", 500));
    fireEvent.click(btn("Refresh"));
    expect(await screen.findByText(/fresh line/)).toBeInTheDocument();
    expect(vi.mocked(mcpServers.runtimeLogs).mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("shows cluster unreachable on a 502 from logs", async () => {
    vi.mocked(mcpServers.runtimeLogs).mockRejectedValue(new ApiError(502, "bad gateway"));
    render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    expect(await screen.findByText("Cluster unreachable")).toBeInTheDocument();
  });

  describe("polling and recovery", () => {
    afterEach(() => { vi.useRealTimers(); });
    const tick = async (ms: number) => { await rtlAct(async () => { await vi.advanceTimersByTimeAsync(ms); }); };

    it("polls a stopping state every 3 s and stops once running", async () => {
      vi.useFakeTimers();
      vi.mocked(mcpServers.runtime).mockResolvedValue(runtime("stopping"));
      render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
      await tick(0);
      expect(mcpServers.runtime).toHaveBeenCalledTimes(1);
      await tick(3000);
      expect(mcpServers.runtime).toHaveBeenCalledTimes(2);
      vi.mocked(mcpServers.runtime).mockResolvedValue(runtime("running"));
      await tick(3000);
      expect(screen.getByTestId("runtime-state")).toHaveTextContent("Running");
      const calls = vi.mocked(mcpServers.runtime).mock.calls.length;
      await tick(9000);
      expect(mcpServers.runtime).toHaveBeenCalledTimes(calls);
    });

    it("does not poll a settled state and skips ticks while hidden", async () => {
      vi.useFakeTimers();
      render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
      await tick(9000);
      expect(mcpServers.runtime).toHaveBeenCalledTimes(1);
      vi.mocked(mcpServers.runtime).mockResolvedValue(runtime("starting"));
      fireEvent.click(screen.getByRole("button", { name: "Refresh state" }));
      await tick(0);
      const calls = vi.mocked(mcpServers.runtime).mock.calls.length;
      const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
      await tick(6000);
      expect(mcpServers.runtime).toHaveBeenCalledTimes(calls);
      hidden.mockRestore();
    });

    it("clears the interval on unmount", async () => {
      vi.useFakeTimers();
      vi.mocked(mcpServers.runtime).mockResolvedValue(runtime("pending"));
      const { unmount } = render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
      await tick(0);
      unmount();
      const calls = vi.mocked(mcpServers.runtime).mock.calls.length;
      await tick(9000);
      expect(mcpServers.runtime).toHaveBeenCalledTimes(calls);
    });
  });

  it("the refresh button reloads the runtime state", async () => {
    render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    await screen.findByTestId("runtime-state");
    vi.mocked(mcpServers.runtime).mockResolvedValue(runtime("stopped"));
    fireEvent.click(btn("Refresh state"));
    await waitFor(() => expect(screen.getByTestId("runtime-state")).toHaveTextContent("Stopped"));
  });

  it("after a 502 action it reloads the runtime, calls onChanged and shows the cluster message", async () => {
    vi.mocked(mcpServers.runtimeAction).mockRejectedValue(new ApiError(502, "kube said no"));
    const onChanged = vi.fn();
    render(<RuntimePanel serverId="s1" enabled onChanged={onChanged} />);
    await screen.findByTestId("runtime-state");
    vi.mocked(mcpServers.runtime).mockResolvedValue(runtime("failed", { last_error: "boom" }));
    fireEvent.click(btn("Restart"));
    expect(await screen.findByText("The cluster could not be reached. Try again.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("runtime-state")).toHaveTextContent("Failed"));
    expect(onChanged).toHaveBeenCalled();
  });

  it("maps a non-JSON failure to the cluster message", async () => {
    vi.mocked(mcpServers.runtimeAction).mockRejectedValue(new SyntaxError("Unexpected token < in JSON"));
    render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    await screen.findByTestId("runtime-state");
    fireEvent.click(btn("Restart"));
    expect(await screen.findByText("The cluster could not be reached. Try again.")).toBeInTheDocument();
  });

  it("ignores a slow runtime response after the server changed", async () => {
    let resolveSlow: (value: McpRuntime) => void = () => {};
    vi.mocked(mcpServers.runtime).mockImplementation((id: string) =>
      id === "s1" ? new Promise<McpRuntime>((resolve) => { resolveSlow = resolve; }) : Promise.resolve(runtime("stopped")));
    const { rerender } = render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    rerender(<RuntimePanel serverId="s2" enabled onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId("runtime-state")).toHaveTextContent("Stopped"));
    await rtlAct(async () => { resolveSlow(runtime("running")); });
    expect(screen.getByTestId("runtime-state")).toHaveTextContent("Stopped");
  });

  it("has no axe violations", async () => {
    const { container } = render(<RuntimePanel serverId="s1" enabled onChanged={vi.fn()} />);
    await screen.findByText(/line one/);
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
