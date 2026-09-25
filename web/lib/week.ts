// Week view model (docs/phases/P6.md §4, §5, step 5). It joins the week's schedule (Q2)
// with its flattened cards (Q3) into display rows. There's no arithmetic on the data:
// it only selects, labels, and decides which state a row is in.
//
// There is deliberately no edge field on a row (§8 Q1). A projection is only ever
// produced together with its stability bucket (§5 device 3).
import type { Game, WeekCard } from "./db";
import { favoredTeam, formatEtClock, formatGameday } from "./format";

/** First week the synthesizer ran live (docs/phases/P5.md). Earlier games never got a
 *  card. */
export const SYNTHESIZER_LIVE_FROM = { season: 2026, week: 3 } as const;

export type Bucket = "low" | "mid" | "high";

export type RowStatus =
  | { kind: "locked"; at: string }
  | { kind: "provisional"; locksFrom: string }
  | { kind: "not_locked" } // kicked off without a lock
  | { kind: "not_projected"; label: string }
  | { kind: "no_card"; reason: string; warn: boolean };

export interface WeekRow {
  gameId: string;
  home: string;
  away: string;
  neutral: boolean;
  gametime: string | null; // ET, as given
  projection: { spreadHome: number; total: number; bucket: Bucket; stabilityMin: number } | null;
  market: { spreadHome: number | null; total: number | null };
  /** The model and the market favor different teams. A boolean on purpose: a flip over a
   *  0.2-point gap and one over 10 points are marked identically (user decision
   *  2026-09-24, option A). Never a difference value (§8 Q1). A pick'em on either side
   *  is not a flip. */
  favoriteFlipped: boolean;
  status: RowStatus;
  venue: {
    roofCode: number | null;
    weatherStatus: number | null;
    temperatureF: number | null;
    windMph: number | null;
    kickedOff: boolean;
  };
}

export interface WeekDay {
  gameday: string | null;
  label: string;
  rows: WeekRow[];
}

interface Context {
  now: Date;
  today: string; // ET calendar day
  fitSeasons: readonly number[];
}

function noCardReason(g: Game, ctx: Context): { reason: string; warn: boolean } {
  if (ctx.fitSeasons.includes(g.season)) {
    return { reason: "no card · in-sample season (used to fit the model)", warn: false };
  }
  const before =
    g.season < SYNTHESIZER_LIVE_FROM.season ||
    (g.season === SYNTHESIZER_LIVE_FROM.season && g.week < SYNTHESIZER_LIVE_FROM.week);
  if (before) {
    return { reason: "no card · predates the synthesizer (2026 wk 3)", warn: false };
  }
  if (g.gameday !== null && g.gameday >= ctx.today) {
    return { reason: "no card yet · built within 7 days of kickoff", warn: false };
  }
  // A played game after go-live with no card at all is unexpected.
  return { reason: "no card", warn: true };
}

function status(c: WeekCard, kickedOff: boolean): RowStatus {
  if (c.projection_status !== 1) {
    return { kind: "not_projected", label: c.projection_status_label ?? `status ${c.projection_status}` };
  }
  if (c.locked && c.locked_at) return { kind: "locked", at: formatEtClock(c.locked_at) };
  if (kickedOff) return { kind: "not_locked" };
  return { kind: "provisional", locksFrom: c.locks_from ? formatEtClock(c.locks_from) : "—" };
}

export function buildWeekRow(g: Game, c: WeekCard | undefined, ctx: Context): WeekRow {
  const kickedOff = c ? new Date(c.kickoff) <= ctx.now : false;
  const base = {
    gameId: g.game_id,
    home: g.home_team,
    away: g.away_team,
    neutral: g.location === "Neutral",
    gametime: g.gametime,
  };
  if (!c) {
    return {
      ...base,
      projection: null,
      market: { spreadHome: null, total: null },
      favoriteFlipped: false,
      status: { kind: "no_card", ...noCardReason(g, ctx) },
      venue: { roofCode: null, weatherStatus: null, temperatureF: null, windMph: null, kickedOff },
    };
  }
  let st = status(c, kickedOff);
  let projection: WeekRow["projection"] = null;
  if (st.kind !== "not_projected") {
    if (
      c.projected_spread !== null &&
      c.projected_total !== null &&
      c.stability_bucket !== null &&
      c.stability_min !== null
    ) {
      projection = {
        spreadHome: c.projected_spread,
        total: c.projected_total,
        bucket: c.stability_bucket,
        stabilityMin: c.stability_min,
      };
    } else {
      // Never show a projection without its stability bucket (§5).
      st = { kind: "not_projected", label: "projection incomplete on card" };
    }
  }
  const marketFav =
    c.market_spread_latest === null
      ? null
      : favoredTeam(c.market_spread_latest, g.home_team, g.away_team, "market");
  const modelFav =
    projection === null ? null : favoredTeam(projection.spreadHome, g.home_team, g.away_team, "model");
  return {
    ...base,
    projection,
    market: { spreadHome: c.market_spread_latest, total: c.market_total_latest },
    favoriteFlipped: marketFav !== null && modelFav !== null && marketFav !== modelFav,
    status: st,
    venue: {
      roofCode: c.venue_roof_code,
      weatherStatus: c.weather_status,
      temperatureF: c.temperature_f,
      windMph: c.wind_speed_mph,
      kickedOff,
    },
  };
}

/** Rows grouped by ET game day, in schedule order (Q2's order is kept). */
export function buildWeek(games: Game[], cards: WeekCard[], ctx: Context): WeekDay[] {
  const byId = new Map(cards.map((c) => [c.game_id, c]));
  const days: WeekDay[] = [];
  for (const g of games) {
    const row = buildWeekRow(g, byId.get(g.game_id), ctx);
    const last = days.at(-1);
    if (last && last.gameday === g.gameday) {
      last.rows.push(row);
    } else {
      days.push({ gameday: g.gameday, label: g.gameday ? formatGameday(g.gameday) : "date TBD", rows: [row] });
    }
  }
  return days;
}
