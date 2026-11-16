import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EMPTY_MODEL_SETTINGS_TEXT, settingsBody, type ModelSettingsText } from "@/lib/modelSettings";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { ModelSettingsPopover, modelSettingsInvalid } from "./ModelSettingsPopover";
import { classifyTurnError, turnBody } from "./runOptions";

function Host({ turnErrors = null, onValue }: { turnErrors?: Record<string, string> | null; onValue?: (v: ModelSettingsText) => void }) {
  const [value, setValue] = React.useState<ModelSettingsText>(EMPTY_MODEL_SETTINGS_TEXT);
  const [open, setOpen] = React.useState(false);
  return (
    <ModelSettingsPopover
      value={value}
      onChange={(next) => {
        setValue(next);
        onValue?.(next);
      }}
      turnErrors={turnErrors}
      open={open}
      onOpenChange={setOpen}
    />
  );
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});

const button = () => screen.getByRole("button", { name: /^Model settings:/ });
const openPanel = () => {
  fireEvent.click(button());
  return screen.getByTestId("model-settings-popover");
};

describe("ModelSettingsPopover", () => {
  it("is a button in the tab order whose accessible name says what is set", () => {
    render(<Host />);
    expect(button()).toHaveAccessibleName("Model settings: using the agent's values");
    expect(button().tabIndex).toBe(0);
    expect(button()).toHaveAttribute("aria-controls");
  });

  it("opens a labelled panel with every setting as a labelled number field, empty by default", () => {
    render(<Host />);
    const panel = openPanel();
    for (const label of [/^Temperature/, /^Max tokens/, /^Top P/, /^Top K/, /^Frequency penalty/, /^Presence penalty/, /^Seed/, /^Max steps/, /^Max retries/]) {
      const input = within(panel).getByLabelText(label);
      expect(input).toHaveAttribute("type", "number");
      expect(input).toHaveValue(null);
    }
    expect(panel).toHaveAccessibleName("Model settings");
  });

  it("counts set fields in the badge and the name, and Clear empties them", () => {
    render(<Host />);
    const panel = openPanel();
    fireEvent.change(within(panel).getByLabelText(/^Top P/), { target: { value: "0.5" } });
    fireEvent.change(within(panel).getByLabelText(/^Seed/), { target: { value: "9" } });
    expect(button()).toHaveAccessibleName("Model settings: 2 set for this chat");
    expect(screen.getByTestId("composer-model-settings-badge")).toHaveTextContent("2");
    fireEvent.click(within(panel).getByRole("button", { name: "Clear" }));
    expect(button()).toHaveAccessibleName("Model settings: using the agent's values");
  });

  it("shows an inline message next to an out-of-range field and marks the button", () => {
    render(<Host />);
    const panel = openPanel();
    const topP = within(panel).getByLabelText(/^Top P/);
    fireEvent.change(topP, { target: { value: "1.5" } });
    expect(topP).toHaveAttribute("aria-invalid", "true");
    expect(topP).toHaveAccessibleDescription(expect.stringContaining("Top P must be 0 to 1"));
    expect(button()).toHaveAccessibleName("Model settings: 1 set for this chat (problems)");
    fireEvent.change(topP, { target: { value: "0.5" } });
    expect(topP).not.toHaveAttribute("aria-invalid");
  });

  it("shows the API's refusal for a field and for the settings as a whole", () => {
    render(<Host turnErrors={{ top_k: "model 'openai:gpt-5' does not accept a top_k setting", "": "Not accepted" }} />);
    const panel = openPanel();
    expect(within(panel).getByLabelText(/^Top K/)).toHaveAccessibleDescription(expect.stringContaining("does not accept a top_k"));
    expect(screen.getByTestId("model-settings-turn-error")).toHaveTextContent("Not accepted");
  });

  it("can be opened from the keyboard and closed with Escape, returning focus to the button", async () => {
    render(<Host />);
    button().focus();
    expect(document.activeElement).toBe(button());
    // A native button: Enter/Space fire its click, which toggles the panel.
    fireEvent.click(button());
    expect(screen.getByTestId("model-settings-popover")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByTestId("model-settings-popover"), { key: "Escape" });
    await act(async () => undefined);
    expect(screen.queryByTestId("model-settings-popover")).not.toBeInTheDocument();
    await waitFor(() => expect(document.activeElement).toBe(button()));
  });

  it("has no axe violations open, with a problem showing", async () => {
    const { container } = render(<Host />);
    const panel = openPanel();
    fireEvent.change(within(panel).getByLabelText(/^Seed/), { target: { value: "1.5" } });
    await expectNoAxeViolations(document.body);
    expect(container).toBeTruthy();
  }, AXE_TIMEOUT_MS);
});

describe("what the chat sends", () => {
  it("sends the set settings as numbers under model_settings, and nothing when empty", () => {
    const typed = { ...EMPTY_MODEL_SETTINGS_TEXT, temperature: "0.3", top_k: "20", max_retries: "0" };
    expect(modelSettingsInvalid(typed)).toBe(false);
    const options = { version: null, model: null, request_context: null, model_settings: settingsBody(typed) };
    expect(turnBody("r1", "hi", options)).toEqual({ run_id: "r1", input: "hi", model_settings: { temperature: 0.3, top_k: 20, max_retries: 0 } });
    expect(turnBody("r1", "hi", { ...options, model_settings: settingsBody(EMPTY_MODEL_SETTINGS_TEXT) })).toEqual({ run_id: "r1", input: "hi" });
  });

  it("maps the API's refusals onto the settings", () => {
    const options = { version: null, model: null, request_context: null, model_settings: { top_k: 3 } };
    expect(classifyTurnError(422, "model 'openai:gpt-5' does not accept a top_k setting; remove top_k from the run's model settings or choose another model", options).modelSettings).toEqual({
      top_k: expect.stringContaining("does not accept a top_k"),
    });
    expect(classifyTurnError(422, [{ loc: ["body", "model_settings", "top_p"], msg: "must be at most 1", type: "less_than_equal" }], options).modelSettings).toEqual({ top_p: "must be at most 1" });
  });
});
