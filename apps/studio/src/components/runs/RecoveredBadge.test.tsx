import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { RecoveredBadge } from "./RecoveredBadge";

afterEach(() => cleanup());

describe("RecoveredBadge", () => {
  it("is absent for a run DBOS never resumed, and without a run", () => {
    const { rerender } = render(<RecoveredBadge run={{ recovered: false, recovery_attempts: 1 }} />);
    expect(screen.queryByTestId("run-recovered-badge")).toBeNull();
    rerender(<RecoveredBadge run={{}} />);
    expect(screen.queryByTestId("run-recovered-badge")).toBeNull();
    rerender(<RecoveredBadge run={null} />);
    expect(screen.queryByTestId("run-recovered-badge")).toBeNull();
  });

  it("says Recovered with the singular tooltip after one restart", () => {
    render(<RecoveredBadge run={{ recovered: true, recovery_attempts: 2 }} />);
    const badge = screen.getByTestId("run-recovered-badge");
    expect(badge).toHaveTextContent(/^Recovered$/);
    expect(badge).toHaveAttribute("title", "Resumed by DBOS after 1 restart; completed steps were not re-run");
  });

  it("adds the count from the second restart on", () => {
    render(<RecoveredBadge run={{ recovered: true, recovery_attempts: 3 }} />);
    const badge = screen.getByTestId("run-recovered-badge");
    expect(badge).toHaveTextContent("Recovered ×2");
    expect(badge).toHaveAttribute("title", "Resumed by DBOS after 2 restarts; completed steps were not re-run");
  });
});
