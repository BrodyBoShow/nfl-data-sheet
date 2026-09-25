import type { NextConfig } from "next";

// cacheComponents stays off (docs/phases/P6.md §1). Pages use route-segment
// `revalidate` for time-based ISR, which is a build error with it on.
const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // `next dev` would otherwise write web/AGENTS.md and web/CLAUDE.md on every start: a
  // generic "Next 16 differs from your training data" note that would sit under the
  // repo's own CLAUDE.md. Versions are pinned and docs checked per P6.md §1 instead.
  agentRules: false,
};

export default nextConfig;
