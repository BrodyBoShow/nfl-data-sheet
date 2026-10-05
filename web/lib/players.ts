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
import type { PlayerEff, PlayerUsage } from "./db";
import { formatFixed } from "./format";
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
    case "seconds":
    case "stab":
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
  "Stability: the share of the season value that isn't league average (0–1). It applies to " +
  "every season value in this row.";

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

/** "2025" or "2023-2025" → [2025] / [2023, 2024, 2025]; null if unparseable. */
export function parseSpan(span: string | null): number[] | null {
  const m = span?.match(/^(\d{4})(?:-(\d{4}))?$/);
  if (!m) return null;
  const first = Number(m[1]);
  const last = Number(m[2] ?? m[1]);
  if (last < first) return null;
  return Array.from({ length: last - first + 1 }, (_, i) => first + i);
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
