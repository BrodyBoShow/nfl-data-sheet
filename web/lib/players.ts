// Players section view model (docs/phases/P7.md step 9; P6.md §2 Q7/Q8). It selects,
// groups, orders and formats the analysts' stored values; nothing here computes a metric.
// The only arithmetic is display scaling: rates ×100 to show as %, per-snap rates ×100 to
// show per 100 snaps.
//
// Decisions it implements (user, 2026-09-30 and 2026-10-01):
// - Player values are never dimmed. Stability is shown as a number, beside the values.
// - Rows are ordered by role (the family's volume, or snap share), never by percentile.
// - Every Pro Football Reference, Next Gen Stats or FTN column names its source in its
//   header (`tags`); /sources carries the full statements.
// - Red-zone shares are season only: a single-game red-zone share rests on one or two
//   touches and carries no stability.
import minimums from "@/content/pct-minimums.json";

import type { PlayerEff, PlayerUsage } from "./db";
import { formatFixed, formatOwnPct } from "./format";
import type { SourceId } from "./sources";

/** A provider credited at the column head. nflverse play-by-play and player stats are
 *  credited site-wide (footer, /sources) and carry no tag. */
export type Tag = "PFR" | "NGS" | "FTN";

export const TAGS: Record<Tag, { source: SourceId; title: string }> = {
  PFR: { source: "pfr", title: "Pro Football Reference (Sports Reference LLC), via nflverse" },
  NGS: { source: "ngs", title: "NFL Next Gen Stats, via nflverse" },
  FTN: { source: "ftn", title: "FTN Data, via nflverse (CC BY-SA 4.0)" },
};

export type Fmt = "pct" | "epa" | "yards" | "seconds" | "points" | "per100" | "count" | "stab" | "rank";

/** Display only. Rates are stored as 0–1 fractions; per-snap rates as per snap. */
export function formatPlayerValue(fmt: Fmt, v: number | null | undefined): string | null {
  if (v == null) return null;
  switch (fmt) {
    case "pct":
      return `${formatFixed(v * 100, 1)}%`;
    case "per100":
      return formatFixed(v * 100, 1);
    case "epa":
      return formatFixed(v, 3);
    case "yards":
    case "points":
      return formatFixed(v, 1);
    case "stab":
      return formatOwnPct(v);
    case "seconds":
      return formatFixed(v, 2);
    case "count":
    case "rank":
      return formatFixed(v, 0);
  }
}

export interface Col<R> {
  key: keyof R & string;
  label: string;
  fmt: Fmt;
  tags: Tag[];
  /** Mixed-source rate (docs/signals.md †): read as approximate. */
  approx?: boolean;
  title?: string;
}

// ---- Role (player_usage_week) ----------------------------------------------------------

export interface ShareGroup {
  label: string;
  tags: Tag[];
  season: keyof PlayerUsage & string;
  last: (keyof PlayerUsage & string) | null; // null: season only
  title?: string;
}

export const ROLE_GROUPS: ShareGroup[] = [
  { label: "Snap %", tags: ["PFR"], season: "off_snap_share_std", last: "off_snap_share_game" },
  { label: "Target share", tags: [], season: "target_share_std", last: "target_share_game" },
  { label: "Air-yards share", tags: [], season: "air_yards_share_std", last: "air_yards_share_game" },
  { label: "Carry share", tags: [], season: "carry_share_std", last: "carry_share_game" },
  {
    label: "RZ target share",
    tags: [],
    season: "rz_target_share_std",
    last: null,
    title: "Season only: a single game's red-zone share rests on one or two touches.",
  },
  {
    label: "RZ carry share",
    tags: [],
    season: "rz_carry_share_std",
    last: null,
    title: "Season only: a single game's red-zone share rests on one or two touches.",
  },
];

// ---- Families (player_eff_week) --------------------------------------------------------

type EffKey = keyof PlayerEff & string;

export interface Family {
  id: "passing" | "rushing" | "receiving" | "defense";
  title: string;
  sample: { std: EffKey; l4: EffKey; label: string; tags: Tag[] };
  stability: { key: EffKey; tags: Tag[]; title: string };
  headline: {
    std: EffKey;
    pct: EffKey;
    l4: EffKey;
    label: string;
    fmt: Fmt;
    tags: Tag[];
    /** Withhold the last-4 value below this last-4 sample (see TACKLE_L4_MIN_DEF_SNAPS). */
    l4MinSample?: number;
  };
  cols: Col<PlayerEff>[];
}

/** PFR's tackles include special-teams coverage tackles, but the denominator is defensive
 *  snaps (docs/phases/P7.md open item 15). Below 80 defensive snaps in the last-4 window,
 *  special teams made up more than ~10% of the shown rate in 2025's 4-game windows (12.7%
 *  at 60-79), so the value isn't shown. A display mitigation, not the fix: item 15 stays
 *  open, and the blended season value is unchanged. */
export const TACKLE_L4_MIN_DEF_SNAPS = 80;

/** The family's last-4 headline value, or null where its last-4 sample is under the
 *  family's floor (a null sample counts as under it). */
export function headlineL4(family: Family, row: PlayerEff): number | null {
  const v = row[family.headline.l4] as number | null;
  const min = family.headline.l4MinSample;
  if (min === undefined) return v;
  const n = row[family.sample.l4] as number | null;
  return n !== null && n >= min ? v : null;
}

const STAB_TITLE =
  "Own %: the share of this row's headline season value that is the player's own " +
  "plays, this season and last, rather than the position group's average. It covers the " +
  "headline column only: every other season column blends at its own rate.";

/** The header label of a family's last-4 sample: the unit, never a bare "n" (P11 C4). */
export function l4SampleLabel(family: Family): string {
  return `L4 ${family.sample.label.toLowerCase()}`;
}

/** Marks, at the column head, a value that is a blended estimate rather than an observed
 *  rate (docs/signals.md, "Prior blend"): every player_eff_week `_std` metric, and the Pct
 *  that ranks the headline one. Family sample counts, usage shares and last-4 values are
 *  observed and carry no mark. The legend under BLEND_MARK explains it once per section. */
export const BLEND_MARK = "*";

export const BLEND_TITLE =
  "Estimate, not an observed rate: season to date, blended with last season and the " +
  "position group's season-to-date average.";

/** A family column's header label: the blend mark, then † for a mixed-source rate. */
export function colLabel(c: Col<PlayerEff>): string {
  return `${c.label} ${BLEND_MARK}${c.approx ? "†" : ""}`;
}

export const FAMILIES: Family[] = [
  {
    id: "passing",
    title: "Passing",
    sample: { std: "pass_dropbacks_std", l4: "pass_dropbacks_l4", label: "Dropbacks", tags: [] },
    stability: { key: "pass_stability", tags: [], title: STAB_TITLE },
    headline: {
      std: "epa_per_dropback_std",
      pct: "epa_per_dropback_pct",
      l4: "epa_per_dropback_l4",
      label: "EPA/dropback",
      fmt: "epa",
      tags: [],
    },
    cols: [
      { key: "dropback_success_rate_std", label: "Success", fmt: "pct", tags: [] },
      { key: "cpoe_std", label: "CPOE (pts)", fmt: "points", tags: [] },
      { key: "pass_adot_std", label: "aDOT (yd)", fmt: "yards", tags: [] },
      { key: "sack_rate_std", label: "Sack %", fmt: "pct", tags: [] },
      {
        key: "pressure_rate_std",
        label: "Pressure %",
        fmt: "pct",
        tags: ["PFR"],
        approx: true,
        title: "PFR pressures over play-by-play dropbacks: two providers, so approximate.",
      },
      { key: "avg_time_to_throw_std", label: "Time to throw (s)", fmt: "seconds", tags: ["NGS"] },
      { key: "scramble_rate_std", label: "Scramble %", fmt: "pct", tags: [] },
    ],
  },
  {
    id: "rushing",
    title: "Rushing",
    sample: { std: "rush_carries_std", l4: "rush_carries_l4", label: "Carries", tags: [] },
    stability: { key: "rush_stability", tags: [], title: STAB_TITLE },
    headline: {
      std: "epa_per_carry_std",
      pct: "epa_per_carry_pct",
      l4: "epa_per_carry_l4",
      label: "EPA/carry",
      fmt: "epa",
      tags: [],
    },
    cols: [
      { key: "rush_success_rate_std", label: "Success", fmt: "pct", tags: [] },
      { key: "stuff_rate_std", label: "Stuff %", fmt: "pct", tags: [] },
      { key: "rush_explosive_rate_std", label: "Explosive %", fmt: "pct", tags: [] },
      {
        key: "yards_before_contact_per_carry_std",
        label: "Yds before contact",
        fmt: "yards",
        tags: ["PFR"],
      },
    ],
  },
  {
    id: "receiving",
    title: "Receiving",
    sample: { std: "rec_targets_std", l4: "rec_targets_l4", label: "Targets", tags: [] },
    stability: { key: "rec_stability", tags: [], title: STAB_TITLE },
    headline: {
      std: "epa_per_target_std",
      pct: "epa_per_target_pct",
      l4: "epa_per_target_l4",
      label: "EPA/target",
      fmt: "epa",
      tags: [],
    },
    cols: [
      { key: "rec_success_rate_std", label: "Success", fmt: "pct", tags: [] },
      { key: "yards_per_target_std", label: "Yds/target", fmt: "yards", tags: [] },
      { key: "rec_adot_std", label: "aDOT (yd)", fmt: "yards", tags: [] },
      { key: "yac_oe_per_reception_std", label: "YAC over exp. (yd)", fmt: "yards", tags: [] },
      { key: "avg_separation_std", label: "Separation (yd)", fmt: "yards", tags: ["NGS"] },
    ],
  },
  {
    id: "defense",
    title: "Defense",
    // Every defense rate is per defensive snap, and snaps are Pro Football Reference's.
    sample: { std: "def_snaps_std", l4: "def_snaps_l4", label: "Snaps", tags: ["PFR"] },
    stability: {
      key: "def_stability",
      tags: ["PFR"],
      title:
        STAB_TITLE +
        " Defense stability rests on tackles, counted over games with a Pro Football Reference defensive row.",
    },
    headline: {
      std: "tackles_per_snap_std",
      pct: "tackles_per_snap_pct",
      l4: "tackles_per_snap_l4",
      label: "Tackles /100 snaps",
      fmt: "per100",
      tags: ["PFR"],
      l4MinSample: TACKLE_L4_MIN_DEF_SNAPS,
    },
    cols: [
      { key: "tfl_per_snap_std", label: "TFL /100 snaps", fmt: "per100", tags: ["PFR"] },
      { key: "pressures_per_snap_std", label: "Pressures /100 snaps", fmt: "per100", tags: ["PFR"] },
      { key: "sacks_per_snap_std", label: "Sacks /100 snaps", fmt: "per100", tags: ["PFR"] },
      { key: "qb_hits_per_snap_std", label: "QB hits /100 snaps", fmt: "per100", tags: ["PFR"] },
      {
        key: "targets_per_snap_std",
        label: "Targeted /100 snaps",
        fmt: "per100",
        tags: ["PFR"],
        title: "Targeted as PFR's nearest defender. Charting on targeted plays, not coverage assignments.",
      },
      {
        key: "yards_per_target_allowed_std",
        label: "Yds/target allowed",
        fmt: "yards",
        tags: ["PFR"],
        title: "As PFR's nearest defender. Charting on targeted plays, not coverage assignments.",
      },
      { key: "missed_tackle_rate_std", label: "Missed tackle %", fmt: "pct", tags: ["PFR"] },
    ],
  },
];

// ---- Percentile population -------------------------------------------------------------

/** What a family's per-game minimum counts (the analyst's _FAMILY_SAMPLE). */
const PCT_UNIT: Record<Family["id"], string> = {
  passing: "dropbacks",
  rushing: "carries",
  receiving: "targets",
  defense: "defensive snaps",
};

const GROUP_PLURAL: Record<string, string> = {
  QB: "QBs",
  RB: "RBs",
  WR: "WRs",
  TE: "TEs",
  OL: "OL",
  DL: "DL",
  LB: "LBs",
  DB: "DBs",
};

const MIN_BY_GROUP: Record<string, Record<string, number>> = minimums.byGroup;
const MIN_DEFAULT: Record<string, number> = minimums.default;

/** The analyst's percentile minimum per game played for a family and position group
 *  (player_efficiency.py MIN_PER_GAME, else MIN_PER_GAME_DEFAULT; synced at build). */
export function pctMinimum(family: Family["id"], group: string): number {
  return MIN_BY_GROUP[family]?.[group] ?? MIN_DEFAULT[family]!;
}

/** The population a row's percentile ranks within, from the row's own data. The analyst
 *  (as_of_percentiles) ranks a row at week W among the latest rows through W of every
 *  player in its position_group whose latest row meets the family minimum. So rows from
 *  different weeks, or different groups, sit in different populations. null when the row
 *  has no position_group: its percentile can't be labeled, so it's withheld. */
export function pctPopulation(family: Family, row: PlayerEff): string | null {
  const group = row.position_group;
  if (group === null) return null;
  const min = pctMinimum(family.id, group);
  const who = GROUP_PLURAL[group] ?? `${group} players`;
  const n = formatFixed(min, Number.isInteger(min) ? 0 : 1);
  return `${who} averaging ${n}+ ${PCT_UNIT[family.id]} per game played, through week ${row.week}`;
}

/** The row's percentile, or null when it isn't shown: no value, or no population to name. */
export function shownPct(family: Family, row: PlayerEff): number | null {
  return pctPopulation(family, row) === null ? null : ((row[family.headline.pct] as number | null) ?? null);
}

/** The Pos cell: the player's position, and the position group a percentile ranks within
 *  when the two differ (an FB ranked among RBs). The row's own data, never the table's. */
export function posLabel(row: { position: string | null; position_group: string | null }): string | null {
  const { position, position_group: group } = row;
  if (position === null) return group;
  return group !== null && group !== position ? `${position} (${group})` : position;
}

/** The cohort caption of a family table, built from the rows it ranks (P11 A): each
 *  position group present with its own minimum. The week is each row's Last wk. null
 *  when no row is ranked. */
export function cohortCaption(family: Family, rows: PlayerEff[]): string | null {
  const groups = [
    ...new Set(rows.filter((r) => shownPct(family, r) !== null).map((r) => r.position_group!)),
  ].sort();
  if (groups.length === 0) return null;
  const parts = groups.map((g) => {
    const min = pctMinimum(family.id, g);
    return `${GROUP_PLURAL[g] ?? `${g} players`} ${formatFixed(min, Number.isInteger(min) ? 0 : 1)}+`;
  });
  return (
    `Pct ranks each player within their position group (Pos), among players averaging ` +
    `${parts.join(", ")} ${PCT_UNIT[family.id]} per game played, through the row's Last wk.`
  );
}

// ---- Coverage history (participation _hist) --------------------------------------------

export interface HistTable {
  id: "receiving" | "passing";
  title: string;
  n: { key: EffKey; label: string };
  cols: Col<PlayerEff>[];
}

export const HIST_TABLES: HistTable[] = [
  {
    id: "receiving",
    title: "Receivers",
    n: { key: "rec_hist_n", label: "Labeled dropbacks" },
    cols: [
      { key: "epa_per_target_vs_man_hist", label: "EPA/target vs man", fmt: "epa", tags: ["FTN"] },
      { key: "epa_per_target_vs_zone_hist", label: "EPA/target vs zone", fmt: "epa", tags: ["FTN"] },
      {
        key: "target_rate_vs_man_hist",
        label: "Target rate vs man",
        fmt: "pct",
        tags: ["FTN"],
        title: "Targets per on-field dropback against man coverage, not per route.",
      },
      {
        key: "target_rate_vs_zone_hist",
        label: "Target rate vs zone",
        fmt: "pct",
        tags: ["FTN"],
        title: "Targets per on-field dropback against zone coverage, not per route.",
      },
    ],
  },
  {
    id: "passing",
    title: "Quarterbacks",
    n: { key: "pass_hist_n", label: "Labeled dropbacks" },
    cols: [
      { key: "epa_per_dropback_vs_man_hist", label: "EPA/dropback vs man", fmt: "epa", tags: ["FTN"] },
      { key: "epa_per_dropback_vs_zone_hist", label: "EPA/dropback vs zone", fmt: "epa", tags: ["FTN"] },
    ],
  },
];

/** The first season FTN's participation charting covers. Earlier seasons are NFL Next
 *  Gen Stats data with a different attribution (docs/sources.md), so they're not shown. */
export const FTN_FIRST_SEASON = 2023;

/** A row's own hist_span (the analyst's format_seasons): "2025", "2023-2025", or runs
 *  joined by ", " ("2023, 2025") → its seasons, ascending; null if unparseable. */
export function parseSpan(span: string | null): number[] | null {
  if (!span) return null;
  const seasons: number[] = [];
  for (const part of span.split(", ")) {
    const m = part.match(/^(\d{4})(?:-(\d{4}))?$/);
    if (!m) return null;
    const first = Number(m[1]);
    const last = Number(m[2] ?? m[1]);
    if (last < first) return null;
    for (let s = first; s <= last; s++) seasons.push(s);
  }
  const sorted = [...new Set(seasons)].sort((a, b) => a - b);
  return sorted.length === seasons.length && sorted.every((s, i) => s === seasons[i]) ? sorted : null;
}

/** Seasons as the analyst labels them: consecutive runs as "2023-2025", gaps as ", ". */
export function formatSeasons(seasons: number[]): string {
  const runs: number[][] = [];
  for (const s of [...new Set(seasons)].sort((a, b) => a - b)) {
    const run = runs[runs.length - 1];
    if (run && s === run[run.length - 1]! + 1) run.push(s);
    else runs.push([s]);
  }
  return runs.map((r) => (r.length === 1 ? `${r[0]}` : `${r[0]}-${r[r.length - 1]}`)).join(", ");
}

// ---- Selection and order ----------------------------------------------------------------

const byName = (a: { display_name: string | null; player_id: string }, b: typeof a) =>
  (a.display_name ?? a.player_id).localeCompare(b.display_name ?? b.player_id);

/** Larger first, nulls last; ties by name. Role order, never percentile order. */
function byVolume<R extends { display_name: string | null; player_id: string }>(
  value: (r: R) => number | null,
) {
  return (a: R, b: R) => {
    const va = value(a);
    const vb = value(b);
    if (va !== vb) {
      if (va === null) return 1;
      if (vb === null) return -1;
      return vb - va;
    }
    return byName(a, b);
  };
}

export function roleRows(usage: PlayerUsage[], team: string): PlayerUsage[] {
  return usage
    .filter((r) => r.team === team && r.off_snap_share_std !== null)
    .sort(byVolume((r) => r.off_snap_share_std));
}

/** A family's rows: players on `team` with a sample in it (the analyst writes null, never
 *  0, for a role the player never held). Ordered by the sample, i.e. by role. */
export function familyRows(eff: PlayerEff[], team: string, family: Family): PlayerEff[] {
  return eff
    .filter((r) => r.team === team && r[family.sample.std] !== null)
    .sort(byVolume((r) => r[family.sample.std] as number | null));
}

export function histRows(eff: PlayerEff[], team: string, table: HistTable): PlayerEff[] {
  return eff
    .filter((r) => r.team === team && r[table.n.key] !== null)
    .sort(byVolume((r) => r[table.n.key] as number | null));
}

// ---- Default visibility (docs/phases/P11.md, B) ------------------------------------------
//
// A player shows by default when the analyst ranked them in some family: their headline
// percentile is shown. That's the analyst's own per-game-played minimum, read from the
// row, not re-derived here. So the shown set and the ranked set are the same players.
// Defense isn't split: its headline percentile is gated null for everyone (P7 step 7),
// and the ungated defense percentiles aren't granted to the web yet (P11 slice 5).

export const RANKED_FAMILIES: Family[] = FAMILIES.filter((f) => f.id !== "defense");

export interface FamilySplit {
  shown: PlayerEff[];
  /** Not ranked, with a position group: below the family's minimum per game played. */
  belowMinimum: PlayerEff[];
  /** No position group: a percentile can't be labeled, so it's withheld. */
  noGroup: PlayerEff[];
}

export function splitFamilyRows(eff: PlayerEff[], team: string, family: Family): FamilySplit {
  const rows = familyRows(eff, team, family);
  return {
    shown: rows.filter((r) => shownPct(family, r) !== null),
    belowMinimum: rows.filter((r) => shownPct(family, r) === null && r.position_group !== null),
    noGroup: rows.filter((r) => r.position_group === null),
  };
}

export interface RoleSplit {
  shown: PlayerUsage[];
  /** Offensive linemen: no family ranks them, so they get their own collapsed line. */
  offensiveLine: PlayerUsage[];
  /** Everyone else not ranked in passing, rushing or receiving. A usage row with no
   *  efficiency row (no family sample) lands here too, as "not ranked", never as zero. */
  hidden: PlayerUsage[];
}

export function splitRoleRows(usage: PlayerUsage[], eff: PlayerEff[], team: string): RoleSplit {
  const ranked = (u: PlayerUsage) => {
    const e = eff.find((x) => x.player_id === u.player_id);
    return e !== undefined && RANKED_FAMILIES.some((f) => shownPct(f, e) !== null);
  };
  const rows = roleRows(usage, team);
  return {
    shown: rows.filter(ranked),
    offensiveLine: rows.filter((r) => !ranked(r) && r.position_group === "OL"),
    hidden: rows.filter((r) => !ranked(r) && r.position_group !== "OL"),
  };
}

/** "QB 1, RB 2, WR 3": hidden rows by position group, in that order; "no group" last. */
export function countByGroup(rows: { position_group: string | null }[]): string {
  const counts = new Map<string, number>();
  for (const r of rows) {
    const g = r.position_group ?? "no group";
    counts.set(g, (counts.get(g) ?? 0) + 1);
  }
  const order = [...Object.keys(GROUP_PLURAL), "no group"];
  const rank = (g: string) => (order.includes(g) ? order.indexOf(g) : order.length - 1);
  return [...counts]
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
    .map(([g, n]) => `${g} ${n}`)
    .join(", ");
}

/** The defense snap share comes from the usage row; it's shown only when that row is as
 *  of the same week as the efficiency row, so one table row never mixes two weeks. */
export function defSnapShare(eff: PlayerEff, usage: PlayerUsage[]): number | null {
  const u = usage.find((x) => x.player_id === eff.player_id);
  return u && u.week === eff.week ? u.def_snap_share_std : null;
}

/** Every source tag the section can render, for /sources coverage checks. */
export function renderedTags(): Tag[] {
  const tags = new Set<Tag>();
  for (const g of ROLE_GROUPS) g.tags.forEach((t) => tags.add(t));
  for (const f of FAMILIES) {
    [...f.sample.tags, ...f.stability.tags, ...f.headline.tags, ...f.cols.flatMap((c) => c.tags)].forEach((t) =>
      tags.add(t),
    );
  }
  for (const h of HIST_TABLES) h.cols.forEach((c) => c.tags.forEach((t) => tags.add(t)));
  tags.add("PFR"); // the games/last-4 statement and the defense snap share
  return [...tags].sort();
}
