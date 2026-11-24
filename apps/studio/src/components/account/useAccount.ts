"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { loadPicture } from "@/lib/auth/avatar";
import { getTokens, subscribeTokens } from "@/lib/auth/session";
import { loadCapabilities, type IamMe } from "@/lib/iam";

/** The signed-in person (once known), their picture (ZITADEL only) and when the Studio session's token lapses (ZITADEL only). */
export function useAccount(): { me: IamMe | null; picture: string | null; expiresAt: number | null } {
  const [me, setMe] = useState<IamMe | null>(null);
  const [picture, setPicture] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    loadCapabilities()
      .then((caps) => live && setMe(caps.me))
      .catch(() => undefined); // a failed read shows nothing; the next page load asks again
    return () => {
      live = false;
    };
  }, []);
  const zitadel = me?.provider === "zitadel";
  useEffect(() => {
    if (!zitadel) return;
    let live = true;
    void loadPicture().then((url) => live && setPicture(url));
    return () => {
      live = false;
    };
  }, [zitadel]);
  const tokens = useSyncExternalStore(subscribeTokens, getTokens, () => null);
  return { me, picture: zitadel ? picture : null, expiresAt: zitadel && tokens ? tokens.expiresAt : null };
}
