import { describe, expect, it } from "vitest";
import { EMPTY_MODEL_SETTINGS_TEXT, MODEL_SETTING_FIELDS, settingProblem, settingsBody, settingsProblems, settingsText } from "./modelSettings";

describe("model settings ranges", () => {
  it.each([
    ["temperature", ["0", "2", "0.7"], ["-0.1", "2.1", "abc"]],
    ["top_p", ["0", "1", "0.95"], ["-0.01", "1.01"]],
    ["top_k", ["1", "40"], ["0", "1.5", "-3"]],
    ["frequency_penalty", ["-2", "2", "0.5"], ["-2.1", "2.1"]],
    ["presence_penalty", ["-2", "2"], ["3", "-3"]],
    ["seed", ["0", "-7", "42"], ["1.5", "x"]],
    ["max_steps", ["1", "200"], ["0", "201", "2.5"]],
    ["max_retries", ["0", "10"], ["-1", "11", "1.5"]],
    ["max_tokens", ["1", "4096"], ["0", "1.5"]],
  ] as const)("%s", (key, good, bad) => {
    for (const value of good) expect(settingProblem(key, value)).toBeNull();
    for (const value of bad) expect(settingProblem(key, value)).toEqual(expect.stringContaining(MODEL_SETTING_FIELDS.find((f) => f.key === key)!.label));
  });

  it("an empty or blank field is unset, not a problem", () => {
    expect(settingsProblems({ ...EMPTY_MODEL_SETTINGS_TEXT, top_p: "  " })).toEqual({});
  });

  it("sends only the settings that are set, as numbers", () => {
    expect(settingsBody({ ...EMPTY_MODEL_SETTINGS_TEXT, top_p: "0.5", seed: "0", max_steps: " 3 " })).toEqual({ top_p: 0.5, seed: 0, max_steps: 3 });
    expect(settingsBody(EMPTY_MODEL_SETTINGS_TEXT)).toEqual({});
  });

  it("reads stored numbers back as text, null as unset", () => {
    expect(settingsText({ temperature: null, top_k: 5, seed: 0 })).toEqual({ ...EMPTY_MODEL_SETTINGS_TEXT, top_k: "5", seed: "0" });
  });
});
