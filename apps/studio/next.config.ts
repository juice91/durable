import type { NextConfig } from "next";

// Same-origin API access. The browser calls `/api/v1/...` on the origin that served Studio (the image is built
// with NEXT_PUBLIC_EAP_API_URL empty). Behind an ingress, `/api/` can go straight to the API Service;
// when Studio is reached directly (the SSH tunnel) this rewrite forwards the same calls to
// the API. Next bakes rewrite destinations into the build (routes-manifest), so EAP_API_INTERNAL_URL is a BUILD-time
// setting (a Dockerfile ARG defaulting to the in-cluster API Service); unset, as in `next dev`, there is no rewrite.
const apiInternalUrl = (process.env.EAP_API_INTERNAL_URL ?? "").replace(/\/+$/, "");

const nextConfig: NextConfig = {
  output: "standalone",
  // gzip buffers a server-sent event stream; an ingress or proxy can compress public traffic.
  compress: false,
  // The rewrite proxy cuts a proxied request after 30 s by default, which would end a run's SSE stream.
  experimental: { proxyTimeout: 60 * 60 * 1000 },
  async rewrites() {
    if (!apiInternalUrl) return [];
    return [{ source: "/api/:path*", destination: `${apiInternalUrl}/api/:path*` }];
  },
};

export default nextConfig;
