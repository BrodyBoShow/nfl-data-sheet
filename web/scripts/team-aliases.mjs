// Read the pipeline's retired-code map (pipeline/core/team_aliases.py, TEAM_ABBR_ALIASES)
// at build time, so the web app normalizes team codes with the same map the pipeline
// uses, not a second copy that could drift. `games` keeps raw codes (OAK for the
// 2018–19 Raiders) while `signals` holds the normalized ones (LV) (docs/sources.md).
// Throws if the dict can't be found or is empty, which fails the build.

export class AliasError extends Error {
  name = "AliasError";
}

/** @param {string} py  the text of pipeline/core/team_aliases.py
 *  @returns {Record<string, string>} */
export function parseTeamAliases(py) {
  const block = /TEAM_ABBR_ALIASES:\s*dict\[str,\s*str\]\s*=\s*\{([\s\S]*?)\n\}/.exec(py)?.[1];
  if (block === undefined) throw new AliasError("TEAM_ABBR_ALIASES not found in team_aliases.py");
  /** @type {Record<string, string>} */
  const map = {};
  for (const line of block.split("\n")) {
    const code = line.replace(/#.*$/, "").trim();
    if (!code) continue;
    const m = /^"([A-Z]{2,3})"\s*:\s*"([A-Z]{2,3})",?$/.exec(code);
    if (!m) throw new AliasError(`unrecognized TEAM_ABBR_ALIASES line: ${line.trim()}`);
    map[m[1]] = m[2];
  }
  if (Object.keys(map).length === 0) throw new AliasError("TEAM_ABBR_ALIASES is empty");
  return map;
}
