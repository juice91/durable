import { seg } from "./query";

export const processorRoutes = {
  /** `/processors`: the processor catalog list. */
  processors: (): string => "/processors",
  /** `/processors/:type`: one processor type (overview, test console, recent outcomes, used by). */
  processor: (type: string): string => `/processors/${seg(type)}`,
};
