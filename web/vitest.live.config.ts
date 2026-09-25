// Live read-contract check: `npm run test:live`. Never part of `npm test` (CLAUDE.md:
// tests never make live calls). Calls the production lib/db.ts functions against the
// real `web` views, with SUPABASE_URL / SUPABASE_ANON_KEY from the repo-root .env.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

const envPath = fileURLToPath(new URL("../.env", import.meta.url));
const env = Object.fromEntries(
  readFileSync(envPath, "utf8")
    .split(/\r?\n/)
    .filter((l) => /^SUPABASE_(URL|ANON_KEY)=/.test(l))
    .map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]),
);

export default defineConfig({
  test: {
    environment: "node",
    include: ["tests-live/**/*.live.test.ts"],
    env,
    testTimeout: 30_000,
  },
});
