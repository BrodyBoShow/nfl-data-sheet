// Team-code normalization with the pipeline's own alias map
// (pipeline/core/team_aliases.py → content/team-aliases.json at build time, via
// scripts/sync-content.mjs). `games` keeps raw codes (OAK for the 2018–19 Raiders), while
// `signals` holds normalized ones (LV), so queries into signals normalize first.
import aliases from "@/content/team-aliases.json";

const map: Record<string, string> = aliases;

export function normalizeTeam(code: string): string {
  return map[code] ?? code;
}
