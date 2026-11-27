import { act, waitFor } from "@testing-library/react";
import { expect, vi } from "vitest";

/**
 * Resolves once the `fetchModels` calls made so far have answered and React has applied them. Use it before an action
 * that picks a default from the model list while nothing on screen says whether the list has arrived; where a control
 * shows it (a provider Select, a listed option), wait for that instead. Clear the mock between tests.
 */
export async function modelListLoaded(fetchModels: (...args: never[]) => unknown): Promise<void> {
  const mock = vi.mocked(fetchModels);
  await waitFor(() => expect(mock).toHaveBeenCalled());
  await act(async () => {
    await Promise.all(mock.mock.results.map((result) => result.value));
  });
}
