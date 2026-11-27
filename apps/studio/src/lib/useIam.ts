"use client";

import { useEffect, useState } from "react";
import { hasAccessTab, loadCapabilities, type IamCapabilities } from "./iam";

/** The capabilities answer once loaded; undefined while loading, null when it cannot be read. */
export function useCapabilities(): IamCapabilities | null | undefined {
  const [caps, setCaps] = useState<IamCapabilities | null | undefined>(undefined);
  useEffect(() => {
    let live = true;
    Promise.resolve(loadCapabilities()).then(
      (answer) => live && setCaps(answer ?? null),
      () => live && setCaps(null),
    );
    return () => {
      live = false;
    };
  }, []);
  return caps;
}

/** Whether the Access tabs are offered (the policy engine keeps grants: not under `openaccess`). */
export function useAccessEnabled(): boolean {
  return hasAccessTab(useCapabilities() ?? null);
}

/** Whether the policy engine can say why something is refused. */
export function canExplain(caps: IamCapabilities | null | undefined): boolean {
  return Boolean(caps?.policy.features.includes("explain"));
}
