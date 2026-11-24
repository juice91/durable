// The tokens of the signed-in person. In MEMORY ONLY: never localStorage, sessionStorage, a cookie or a log line. A page
// reload forgets them and the sign-in flow runs again (ZITADEL's own session makes that one redirect, no prompt).

export interface Tokens {
  accessToken: string;
  refreshToken?: string;
  idToken?: string;
  /** Epoch milliseconds. */
  expiresAt: number;
}

let current: Tokens | null = null;
const listeners = new Set<() => void>();

export function getTokens(): Tokens | null {
  return current;
}

export function getAccessToken(): string | undefined {
  return current?.accessToken;
}

export function setTokens(tokens: Tokens | null): void {
  current = tokens;
  for (const listener of [...listeners]) listener();
}

export function subscribeTokens(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
