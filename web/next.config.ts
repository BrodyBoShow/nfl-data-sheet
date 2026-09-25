import type { NextConfig } from "next";

// cacheComponents stays off (docs/phases/P6.md §1). Pages use route-segment
// `revalidate` for time-based ISR, which is a build error with it on.
const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
};

export default nextConfig;
