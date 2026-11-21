/**
 * Where the browser reaches the API. Unset keeps the local-development default; an EMPTY value means
 * same-origin (requests go to `/api/v1/...` on the origin that served Studio), which is how the public
 * Cloudflare Tunnel hostname and the Next.js `/api/:path*` rewrite (next.config.ts) work.
 */
export function resolveBaseUrl(raw: string | undefined): string {
  if (raw === undefined) return "http://localhost:8000";
  return raw.trim().replace(/\/+$/, "");
}

export const BASE_URL = resolveBaseUrl(process.env.NEXT_PUBLIC_EAP_API_URL);
