import { afterEach, describe, expect, it, vi } from "vitest";
vi.mock("./api", async (importOriginal) => ({ ...(await importOriginal<object>()), apiHeaders: () => ({}) }));

import { createSchedule, updateSchedule } from "./schedules";

function failWith(status: number, detail: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail }), { status, headers: { "Content-Type": "application/json" } })));
}

afterEach(() => vi.unstubAllGlobals());

describe("schedule API errors", () => {
  const body = { name: "n", cron: "* * * * *", timezone: "UTC", input: {} };

  it.each([
    [409, "schedule_name_taken", "A schedule with this name already exists"],
    [422, "invalid_timezone", "Unknown timezone (use an IANA name such as UTC)"],
    [422, "invalid_cron", "Not a valid 5-field cron expression"],
  ])("maps %s %s to readable copy", async (status, code, message) => {
    failWith(status, { code });
    await expect(createSchedule("w1", body)).rejects.toMatchObject({ message });
  });

  it("prefers the API's message when present", async () => {
    failWith(422, { code: "invalid_cron", message: "minute field out of range" });
    await expect(createSchedule("w1", body)).rejects.toMatchObject({ message: "minute field out of range" });
  });

  it("keeps the input_schema_errors list", async () => {
    failWith(422, { code: "input_schema_errors", errors: ["topic: missing", "n: type"] });
    await expect(updateSchedule("s1", { input: {} })).rejects.toMatchObject({ message: "input_schema_errors: topic: missing; n: type" });
  });

  it("falls back to the code for an unknown one", async () => {
    failWith(422, { code: "something_else" });
    await expect(updateSchedule("s1", { active: true })).rejects.toMatchObject({ message: "something_else" });
  });
});
