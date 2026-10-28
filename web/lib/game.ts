// Game view model (docs/phases/P6.md §5, §6, step 6). Everything shown comes from the
// card, and this module selects, labels and formats it. The edge values are the card's
// own (edge.at_lock / edge.vs_current), never recomputed. Every edge travels with its
// validation status. The card's "model favors …" summary is never used (§5).
import { backtest, type BacktestSummary } from "./backtest";
import type { Card, CardPairing } from "./card";
import {
  favoredTeam,
  formatEtClock,
  formatFixed,
  formatInterval,
  formatLine,
  formatOwnPct,
  formatSeasonRange,
  formatSpread,
} from "./format";

export type Market = "spread" | "total";

/** A context value whose stability is below the lowest model-input stability of any
 *  backtested game. `stability` is the share of the value that isn't the league average
 *  (docs/signals.md), so these are mostly league average. They render dimmed (§5). */
export function isLowStability(stability: number | null | undefined, floor = backtest.stability_floor): boolean {
  return stability != null && stability < floor;
}

export function lowStabilityTitle(stability: number, floor = backtest.stability_floor): string {
  const league = Math.round((1 - stability) * 100);
  return (
    `Own ${formatOwnPct(stability)}: about ${league}% of this number is the league average. ` +
    `Dimmed below ${formatOwnPct(floor)}, the lowest input Own % in the model's backtest.`
  );
}

/** The matchup rows open by default (docs/phases/P11.md, decision 2), in this order. The
 *  other bases collapse under them.
 *  - EPA/play stays though pass + rush nearly derive it: it's the model's only input, so
 *    a reader sees what the projection is built from. Don't optimize it away.
 *  - Red-zone TD rate decides games and none of the others derive it.
 *  Every base here must be an efficiency base the card pairs (game.test.ts checks it). */
export const HEADLINE_PAIRING_BASES = [
  "epa_per_play",
  "epa_per_play_pass",
  "epa_per_play_rush",
  "success_rate",
  "explosive_rate",
  "points_per_drive",
  "red_zone_td_rate",
] as const;

/** The model's own inputs as matchup rows, from the projection's decomposition: each
 *  offense term beside the opponent-defense term of the same base. The card's pairings
 *  leave the model's bases out (synthesizer.py, `exclude_bases=PRIMARY_SPEC.bases`)
 *  because the arithmetic shows them. The arithmetic is collapsed now, and EPA/play is a
 *  headline row (HEADLINE_PAIRING_BASES), so it comes from here. These are the card's own
 *  numbers, frozen with it like every other pairing. Empty without a projection. */
export function modelInputPairings(card: Card, side: "home" | "away"): CardPairing[] {
  const terms = card.projection?.decomposition[side].terms ?? [];
  const brief = (t: (typeof terms)[number]) => ({
    signal: t.signal,
    team: t.team,
    player_id: null,
    value: t.value,
    stability: t.stability,
    sample_n: t.sample_n,
  });
  return terms
    .filter((t) => t.role === "offense" && t.signal.endsWith("_off"))
    .map((o) => {
      const base = o.signal.slice(0, -"_off".length);
      const d = terms.find((t) => t.role === "opponent_defense" && t.signal === `${base}_def`);
      return { base, subject: brief(o), opponent: d ? brief(d) : null };
    });
}

/** The deploy date of Availability's position-code fix, as its rows label it:
 *  `positions@<date>` in `inputs_version` (availability_impact.py, _POSITION_VOCABULARY;
 *  game.test.ts pins the two equal). docs/phases/P7.md, open item 22. */
export const POSITION_VOCABULARY = "2026-10-28";

const CLUSTER_SIGNALS = ["ol_cluster_count", "secondary_cluster_count"];

export interface VocabularyNote {
  /** The game has no cluster-count row of its own; the week's rows decided. Its counts
   *  read "—", which may hide a team whose only flagged players were tackles or SAFs. */
  noRows: boolean;
  /** A Rams cluster-count row is missing too, where the old team-code mismatch (P8 D19)
   *  kept it from being written. */
  ramsMissing: boolean;
}

const predates = (r: { inputs_version: string }) => !r.inputs_version.includes("positions@");

/** Whether this game's availability counts predate the position-code fix, from the rows'
 *  own labels: a cluster-count row without a `positions@` label was counted with the old
 *  code sets.
 *  - The game's own rows decide when it has any.
 *  - With none (both teams blank), the week's rows decide (`weekLabels`, Q6b): every
 *    count that week was written by the same run.
 *  null when every deciding row carries the label, or there is no row to decide from. */
export function availabilityVocabularyNote(
  rows: readonly { sector: string; game_id: string | null; team: string | null; signal: string; inputs_version: string }[],
  teams: readonly string[],
  weekLabels: readonly { team: string | null; inputs_version: string }[] = [],
): VocabularyNote | null {
  const counts = rows.filter(
    (r) => r.sector === "availability" && r.game_id === null && r.team !== null && teams.includes(r.team) &&
      CLUSTER_SIGNALS.includes(r.signal),
  );
  const deciding = counts.length ? counts : weekLabels;
  if (!deciding.some(predates)) return null;
  return {
    noRows: counts.length === 0,
    ramsMissing: teams.includes("LA") && !counts.some((r) => r.team === "LA"),
  };
}

/** The Rank column shows only when some row in the table has a league_pct. */
export function anyLeaguePct(values: readonly (number | null | undefined)[]): boolean {
  return values.some((v) => v != null);
}

export interface ValidationTag {
  validated: boolean;
  evidence: string | null; // backtest figures for this card's bucket; null if model versions differ
}

export interface LinesView {
  locked: boolean;
  marketRows: { label: string; note: string | null; spread: string | null; total: string | null }[];
  model: { label: string; spread: string; total: string; flipped: boolean };
  edge: { spread: string | null; total: string | null; tags: Record<Market, ValidationTag> };
  stability: { bucket: "low" | "mid" | "high"; min: string };
  typicalMiss: { margin: string; total: string };
}

/** The card's spread edge (home-negative, model − market), as distance and direction.
 *  A value that shows as zero reads "none". */
export function edgeSpreadText(edge: number | null, home: string, away: string): string | null {
  if (edge === null) return null;
  const shown = formatFixed(Math.abs(edge), 1);
  if (Number(shown) === 0) return "none";
  return `${shown} toward ${edge < 0 ? home : away}`;
}

/** The card's total edge (model − market). */
export function edgeTotalText(edge: number | null): string | null {
  if (edge === null) return null;
  const shown = formatFixed(Math.abs(edge), 1);
  if (Number(shown) === 0) return "none";
  return `model ${shown} ${edge > 0 ? "higher" : "lower"}`;
}

export function validationTag(card: Card, market: Market, summary: BacktestSummary = backtest): ValidationTag {
  const u = card.uncertainty;
  const validated = u?.edge_validated[market] ?? false;
  const bucket = u?.stability_bucket;
  const ev = bucket ? summary.buckets[bucket]?.[market] : undefined;
  const sameModel = card.projection?.model_version === summary.model_version;
  const evidence =
    ev && sameModel && bucket
      ? `Backtest ${formatSeasonRange(summary.test_seasons)}, ${bucket} input stability ` +
        `(n ${ev.n}): r ${formatFixed(ev.corr, 3)} ${formatInterval([ev.ci_low, ev.ci_high], 3)}.`
      : null;
  return { validated, evidence };
}

/** Null when there is no projection to show (projection_status ≠ 1, or a missing block). */
export function buildLines(card: Card): LinesView | null {
  const p = card.projection;
  const u = card.uncertainty;
  if (card.projection_status !== 1 || !p || !u) return null;
  const { home_team: home, away_team: away } = card.identity;
  const locked = card.lock.locked && card.edge.at_lock !== null;
  const latest = card.edge.vs_current;
  const atLock = card.edge.at_lock;
  const leadHours = card.market.signals.market_current_lead_hours?.value ?? null;
  const latestNote = leadHours === null ? null : `captured ${formatFixed(leadHours, 1)} h before kickoff`;
  const spread = (x: number | null) => (x === null ? null : formatSpread(x, home, away, "market"));
  const total = (x: number | null) => (x === null ? null : formatLine(x));

  const marketRows: LinesView["marketRows"] = locked
    ? [
        {
          label: "Market at lock",
          note: card.lock.locked_at ? `locked ${formatEtClock(card.lock.locked_at)} ET` : null,
          spread: spread(atLock!.market_spread),
          total: total(atLock!.market_total),
        },
        { label: "Latest pre-kickoff market", note: latestNote, spread: spread(latest.market_spread), total: total(latest.market_total) },
      ]
    : [{ label: "Market (latest)", note: latestNote, spread: spread(latest.market_spread), total: total(latest.market_total) }];

  // The claim's edge: vs the lock line once locked, else vs the latest line.
  const edge = locked ? atLock! : latest;
  const compared = edge.market_spread;
  const marketFav = compared === null ? null : favoredTeam(compared, home, away, "market");
  const modelFav = favoredTeam(p.spread_home, home, away, "model");

  return {
    locked,
    marketRows,
    model: {
      label: locked ? "Model (locked)" : "Model (provisional)",
      spread: formatSpread(p.spread_home, home, away, "model"),
      total: formatFixed(p.total, 1),
      flipped: marketFav !== null && modelFav !== null && marketFav !== modelFav,
    },
    edge: {
      spread: edgeSpreadText(edge.spread, home, away),
      total: edgeTotalText(edge.total),
      tags: { spread: validationTag(card, "spread"), total: validationTag(card, "total") },
    },
    stability: { bucket: u.stability_bucket, min: formatFixed(u.stability_min, 2) },
    typicalMiss: {
      margin: String(Math.round(u.outcome_noise.margin_rms)),
      total: String(Math.round(u.outcome_noise.total_rms)),
    },
  };
}

export type GameStatus =
  | { kind: "locked"; at: string; leadHours: string | null }
  | { kind: "provisional"; locksFrom: string | null }
  | { kind: "not_locked" }
  | { kind: "not_projected"; label: string };

export function gameStatus(card: Card, now: Date): GameStatus {
  if (card.projection_status !== 1) return { kind: "not_projected", label: card.projection_status_label };
  if (card.lock.locked && card.lock.locked_at) {
    return {
      kind: "locked",
      at: formatEtClock(card.lock.locked_at),
      leadHours: card.lock.lock_lead_hours === null ? null : formatFixed(card.lock.lock_lead_hours, 1),
    };
  }
  if (new Date(card.identity.kickoff) <= now) return { kind: "not_locked" };
  return { kind: "provisional", locksFrom: card.lock.locks_from ? formatEtClock(card.lock.locks_from) : null };
}
