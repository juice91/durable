// The signed-in person's picture (ZITADEL `picture` claim), for the account menu. Display only: nothing here is a
// credential check, and nothing is persisted. The result is cached in MEMORY for the session and dropped on sign-out.
//
// 1. The `picture` claim of the id token, when ZITADEL put it there (the application's "user info inside the id token" setting).
// 2. Otherwise the OIDC userinfo endpoint with the access token (needs CORS for Studio's origin; a refusal means no picture).
// ZITADEL serves avatars behind the same login, so an image on the issuer's own origin is fetched with the bearer and
// shown from an in-memory blob; an image anywhere else is used as a plain URL. Any failure is "no picture": the caller
// shows the initial.
import { authConfig } from "./controller";
import { getTokens, subscribeTokens } from "./session";

function claims(token: string | undefined): Record<string, unknown> {
  try {
    const body = token?.split(".")[1] ?? "";
    const padded = body.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (body.length % 4)) % 4);
    return JSON.parse(atob(padded)) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function httpsUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : null;
  } catch {
    return null;
  }
}

/** The `picture` URL in an id token, or null. */
export function pictureFromIdToken(idToken: string | undefined): string | null {
  return httpsUrl(claims(idToken).picture);
}

let cached: Promise<string | null> | null = null;
let objectUrl: string | null = null;
let watching = false;

function forget(): void {
  cached = null;
  if (objectUrl && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(objectUrl);
  objectUrl = null;
}

function watchSession(): void {
  if (watching) return;
  watching = true;
  subscribeTokens(() => {
    if (getTokens() === null) forget(); // signed out: the next session starts clean
  });
}

export function resetPictureForTests(): void {
  forget();
}

async function lookup(): Promise<string | null> {
  const config = authConfig();
  const tokens = getTokens();
  if (!config || !tokens) return null;
  let picture = pictureFromIdToken(tokens.idToken);
  if (!picture) {
    const response = await window.fetch(`${config.issuer}/oidc/v1/userinfo`, {
      headers: { Authorization: `Bearer ${tokens.accessToken}`, Accept: "application/json" },
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
    });
    if (!response.ok) return null;
    picture = httpsUrl(((await response.json()) as { picture?: unknown }).picture);
  }
  if (!picture) return null;
  if (new URL(picture).origin !== new URL(config.issuer).origin) return picture;
  const image = await window.fetch(picture, {
    headers: { Authorization: `Bearer ${getTokens()?.accessToken ?? tokens.accessToken}` },
    credentials: "omit",
    referrerPolicy: "no-referrer",
  });
  if (!image.ok || typeof URL.createObjectURL !== "function") return null;
  const blob = await image.blob();
  if (!blob.type.startsWith("image/")) return null;
  objectUrl = URL.createObjectURL(blob);
  return objectUrl;
}

/** The picture to show for this session, or null. Asked once per session; never throws. */
export function loadPicture(): Promise<string | null> {
  watchSession();
  cached ??= lookup().catch(() => null);
  return cached;
}
