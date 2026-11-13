import { describe, expect, it } from "vitest";
import { formatCost, formatTokens, formatUsd, parseCost } from "./format";

describe("cost formatting (one rule for playground, traces, metrics and dashboard)", () => {
  it("shows two significant digits below $1", () => {
    // The same turn read "$0.01" on Metrics and "$0.0149" on Traces before.
    expect(formatCost("0.0149000000")).toBe("$0.015");
    expect(formatCost("0.0015")).toBe("$0.0015");
    expect(formatCost("0.0012")).toBe("$0.0012");
    expect(formatCost("0.0001234567")).toBe("$0.00012");
    expect(formatCost("0.0420000000")).toBe("$0.042");
    expect(formatCost("0.5")).toBe("$0.50");
    expect(formatCost("0.0000000020")).toBe("$0.0000000020");
  });

  it("shows two decimals with grouping at $1 and above, including values that round up to $1", () => {
    expect(formatCost("1.5000000000")).toBe("$1.50");
    expect(formatCost("12.3400000000")).toBe("$12.34");
    expect(formatCost("1234.5")).toBe("$1,234.50");
    expect(formatCost("0.996")).toBe("$1.00");
  });

  it("shows $0 for zero and null for missing or unparsable values", () => {
    expect(formatCost("0")).toBe("$0");
    expect(formatCost("0E-10")).toBe("$0");
    expect(formatCost(0)).toBe("$0");
    expect(formatCost(null)).toBeNull();
    expect(formatCost(undefined)).toBeNull();
    expect(formatCost("")).toBeNull();
    expect(formatCost("abc")).toBeNull();
    expect(parseCost("0.25")).toBe(0.25);
    expect(parseCost("x")).toBeNull();
  });

  it("formats numbers and keeps the sign", () => {
    expect(formatUsd(0.0149)).toBe("$0.015");
    expect(formatUsd(-0.0149)).toBe("-$0.015");
    expect(formatUsd(Number.NaN)).toBe("-");
  });
});

describe("token formatting", () => {
  it("is exact with grouping below a million and compact above", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(34)).toBe("34");
    expect(formatTokens(17_500)).toBe("17,500");
    expect(formatTokens(999_999)).toBe("999,999");
    expect(formatTokens(4_500_000)).toBe("4.5M");
    expect(formatTokens(2_000_000_000)).toBe("2B");
    expect(formatTokens(null)).toBe("-");
    expect(formatTokens(undefined)).toBe("-");
  });
});
