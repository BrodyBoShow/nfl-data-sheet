// Pure parsing for the build-time backtest summary (docs/phases/P6.md §3, §5, step 4).
// No I/O here: sync-content.mjs reads the files, and tests/backtest-content.test.ts
// runs this against the real report.
//
// Every lookup is by exact heading and column name, never by position. Anything
// unexpected (a missing or duplicated heading, a renamed column, a non-numeric cell,
// or the report and the coefficients file disagreeing) throws, which fails the build.
// A status line built from a misread table is worse than no build.

export class ContentError extends Error {
  name = "ContentError";
}

/**
 * The markdown table directly under a unique heading line.
 * @param {string} md
 * @param {string} heading exact heading line, e.g. "### Margin (home − away)"
 * @returns {Record<string, string>[]} rows keyed by header cell
 */
export function tableUnder(md, heading) {
  const lines = md.split(/\r?\n/);
  const at = lines.flatMap((l, i) => (l.trim() === heading ? [i] : []));
  if (at.length !== 1) {
    throw new ContentError(`expected exactly one "${heading}" in the report, found ${at.length}`);
  }
  let i = at[0] + 1;
  while (i < lines.length && !lines[i].trim().startsWith("|")) {
    if (/^#{1,6} /.test(lines[i])) break; // next heading before any table
    i++;
  }
  const rows = [];
  while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(lines[i++]);
  if (rows.length < 3) throw new ContentError(`no table under "${heading}"`);
  const cells = (l) =>
    l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const header = cells(rows[0]);
  return rows.slice(2).map((r) => {
    const c = cells(r);
    if (c.length !== header.length) {
      throw new ContentError(`ragged row under "${heading}": ${r}`);
    }
    return Object.fromEntries(header.map((h, k) => [h, c[k]]));
  });
}

/** @param {Record<string, string>[]} rows */
function rowWhere(rows, column, value, heading) {
  if (!rows.length || !(column in rows[0])) {
    throw new ContentError(`no "${column}" column under "${heading}"`);
  }
  const hit = rows.filter((r) => r[column] === value);
  if (hit.length !== 1) {
    throw new ContentError(`expected one "${value}" row under "${heading}", found ${hit.length}`);
  }
  return hit[0];
}

/** @param {Record<string, string>} row */
function number(row, column, heading) {
  if (!(column in row)) throw new ContentError(`no "${column}" column under "${heading}"`);
  const raw = row[column].replace(/−/g, "-");
  if (!/^-?\d+(\.\d+)?$/.test(raw)) {
    throw new ContentError(`"${column}" under "${heading}" is not a number: ${row[column]}`);
  }
  return Number(raw);
}

/** "[-0.080, 0.016]" → [-0.08, 0.016] */
function interval(row, column, heading) {
  const m = /^\[(-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?)\]$/.exec(
    (row[column] ?? "").replace(/−/g, "-"),
  );
  if (!m) throw new ContentError(`"${column}" under "${heading}" is not an interval`);
  return [Number(m[1]), Number(m[2])];
}

const MARGIN = "### Margin (home − away)";
const SPREAD_CORR = "### Spread: correlation";

/**
 * Build the summary the app renders. Cross-checks the report against the
 * coefficients file, since both come from the same run.
 * @param {string} reportMd docs/backtest_report.md
 * @param {any} coefficients pipeline/synthesis/model_coefficients.json
 */
export function buildSummary(reportMd, coefficients) {
  const margin = tableUnder(reportMd, MARGIN);
  const all = rowWhere(margin, "Season", "**All**", MARGIN);
  const seasons = margin
    .filter((r) => /^\d{4}$/.test(r.Season))
    .map((r) => Number(r.Season));

  const corr = tableUnder(reportMd, SPREAD_CORR);
  const corrAll = rowWhere(corr, "Slice", "All", SPREAD_CORR);
  const validatedCell = corrAll.Validated;
  if (validatedCell !== "yes" && validatedCell !== "no") {
    throw new ContentError(`"Validated" under "${SPREAD_CORR}" is "${validatedCell}"`);
  }

  const cal = coefficients?.calibration;
  if (!cal || !Array.isArray(cal.test_seasons) || typeof cal.n_games !== "number") {
    throw new ContentError("model_coefficients.json has no calibration block");
  }
  const n = number(all, "n", MARGIN);
  const checks = [
    [n === cal.n_games, `margin n ${n} vs calibration.n_games ${cal.n_games}`],
    [number(all, "Close n", MARGIN) === n, "margin Close n differs from n"],
    [number(corrAll, "n", SPREAD_CORR) === n, "spread-correlation n differs from margin n"],
    [
      JSON.stringify(seasons) === JSON.stringify(cal.test_seasons),
      `report seasons ${seasons} vs calibration.test_seasons ${cal.test_seasons}`,
    ],
  ];
  for (const [ok, what] of checks) {
    if (!ok) throw new ContentError(`report and coefficients disagree: ${what}`);
  }

  /** @type {Record<string, {n_games: number, spread: any, total: any}>} */
  const buckets = {};
  /** @type {string[]} */
  const validatedBuckets = [];
  for (const [name, b] of Object.entries(cal.buckets ?? {})) {
    const ev = b?.edge_validation;
    if (!ev?.spread || !ev?.total) throw new ContentError(`bucket ${name} has no edge_validation`);
    buckets[name] = { n_games: b.n_games, spread: ev.spread, total: ev.total };
    for (const market of ["spread", "total"]) {
      if (ev[market].validated === true) validatedBuckets.push(`${name} ${market}`);
    }
  }
  if (!Object.keys(buckets).length) throw new ContentError("calibration has no buckets");
  // The lowest input stability of any backtested game: the bottom of the lowest bucket's
  // stability_range. The game view dims context values below it (P6.md §5).
  const floors = Object.values(cal.buckets).map((b) => b?.stability_range?.[0]);
  if (!floors.every((f) => typeof f === "number" && f > 0 && f < 1)) {
    throw new ContentError("a calibration bucket has no stability_range in (0, 1)");
  }
  const fitSeasons = coefficients.fit_seasons;
  if (!Array.isArray(fitSeasons) || !fitSeasons.every(Number.isInteger)) {
    throw new ContentError("model_coefficients.json has no fit_seasons list");
  }

  return {
    model_version: coefficients.model_version,
    fit_seasons: fitSeasons,
    test_seasons: seasons,
    n_games: n,
    margin_mae: {
      model: number(all, "Model MAE", MARGIN),
      close: number(all, "Close MAE", MARGIN),
    },
    spread_edge_corr: {
      r: number(corrAll, "r", SPREAD_CORR),
      ci: interval(corrAll, "95% CI", SPREAD_CORR),
      n: number(corrAll, "n", SPREAD_CORR),
      validated: validatedCell === "yes",
    },
    validated_buckets: validatedBuckets,
    buckets,
    stability_floor: Math.min(...floors),
  };
}
