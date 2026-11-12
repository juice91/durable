import { beforeAll, expect } from "vitest";
import "@testing-library/jest-dom/vitest";

beforeAll(() => {
  process.env.TZ = "UTC";
});
