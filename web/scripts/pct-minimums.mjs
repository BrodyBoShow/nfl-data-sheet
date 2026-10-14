// Read the Player efficiency analyst's percentile minimums
// (pipeline/analysts/player_efficiency.py, MIN_PER_GAME and MIN_PER_GAME_DEFAULT) at build
// time, so each Pct cell names the same per-game minimum the analyst ranked with, not a
// second copy that could drift. Throws if either dict is missing, has an entry it can't
// read, or lacks a family, which fails the build.

export class MinimumsError extends Error {
  name = "MinimumsError";
}

const FAMILIES = ["passing", "rushing", "receiving", "defense"];

/** The `{...}` body of `name`'s dict literal. Values are numbers, so the first `}` ends it. */
function dictBody(py, name) {
  const body = new RegExp(`^${name}(?::[^=\\n]*)?\\s*=\\s*\\{([^}]*)\\}`, "m").exec(py)?.[1];
  if (body === undefined) throw new MinimumsError(`${name} not found in player_efficiency.py`);
  return body;
}

/** Every `entry` match in `body`, failing on anything left over but commas and whitespace. */
function entries(body, entry, name) {
  const found = [...body.matchAll(new RegExp(entry, "g"))];
  const rest = body.replace(/#.*$/gm, "").replace(new RegExp(entry, "g"), "").replace(/[\s,]/g, "");
  if (rest) throw new MinimumsError(`unrecognized ${name} entry: ${rest}`);
  return found;
}

const number = (s, name) => {
  const v = Number(s);
  if (!Number.isFinite(v) || v <= 0) throw new MinimumsError(`${name}: not a positive number: ${s}`);
  return v;
};

/** @param {string} py  the text of pipeline/analysts/player_efficiency.py
 *  @returns {{ default: Record<string, number>, byGroup: Record<string, Record<string, number>> }} */
export function parsePctMinimums(py) {
  /** @type {Record<string, number>} */
  const byFamily = {};
  for (const m of entries(dictBody(py, "MIN_PER_GAME_DEFAULT"), `"([a-z]+)"\\s*:\\s*([0-9.]+)`, "MIN_PER_GAME_DEFAULT")) {
    byFamily[m[1]] = number(m[2], "MIN_PER_GAME_DEFAULT");
  }
  for (const f of FAMILIES) {
    if (!(f in byFamily)) throw new MinimumsError(`MIN_PER_GAME_DEFAULT has no ${f}`);
  }
  /** @type {Record<string, Record<string, number>>} */
  const byGroup = {};
  for (const m of entries(
    dictBody(py, "MIN_PER_GAME"),
    `\\(\\s*"([a-z]+)"\\s*,\\s*"([A-Z]+)"\\s*\\)\\s*:\\s*([0-9.]+)`,
    "MIN_PER_GAME",
  )) {
    if (!FAMILIES.includes(m[1])) throw new MinimumsError(`MIN_PER_GAME: unknown family ${m[1]}`);
    (byGroup[m[1]] ??= {})[m[2]] = number(m[3], "MIN_PER_GAME");
  }
  return { default: byFamily, byGroup };
}
