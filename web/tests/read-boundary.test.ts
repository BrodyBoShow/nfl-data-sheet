// lib/db.ts is the only module that reads SUPABASE_* / process.env or calls fetch
// (docs/phases/P6.md §7 step 3). Also: no NEXT_PUBLIC_ variable anywhere, since that
// prefix ships a value to the browser, and the anon key stays server-only (§2d).
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const root = fileURLToPath(new URL("..", import.meta.url));
// App source only. scripts/ (fixture capture, layout check) and tests/ are tooling.
const SOURCE_DIRS = ["app", "components", "lib"];
const ALLOWED = "lib/db.ts";

function sourceFiles(dir: string): string[] {
  const abs = join(root, dir);
  let entries: string[];
  try {
    entries = readdirSync(abs);
  } catch {
    return [];
  }
  return entries.flatMap((name) => {
    const p = join(abs, name);
    if (statSync(p).isDirectory()) return sourceFiles(relative(root, p));
    return /\.(ts|tsx|js|jsx|mjs)$/.test(name) ? [relative(root, p).split(sep).join("/")] : [];
  });
}

const files = SOURCE_DIRS.flatMap(sourceFiles);
const RULES: [string, RegExp][] = [
  ["calls fetch", /\bfetch\s*\(/],
  ["reads process.env", /\bprocess\.env\b/],
  // No leading \b: in NEXT_PUBLIC_SUPABASE_URL the "_" before "S" is a word character,
  // so \bSUPABASE_ would miss it (found by the failure demonstration).
  ["names a SUPABASE_ variable", /SUPABASE_[A-Z_]+/],
];

describe("read boundary", () => {
  it("finds the source files it is guarding", () => {
    expect(files).toContain(ALLOWED);
    expect(files.length).toBeGreaterThan(3);
  });

  it.each(RULES)("only lib/db.ts %s", (_, pattern) => {
    const offenders = files.filter(
      (f) => f !== ALLOWED && pattern.test(readFileSync(join(root, f), "utf8")),
    );
    expect(offenders).toEqual([]);
  });

  it("no NEXT_PUBLIC_ variable in app source", () => {
    const offenders = files.filter((f) => /NEXT_PUBLIC_/.test(readFileSync(join(root, f), "utf8")));
    expect(offenders).toEqual([]);
  });
});
