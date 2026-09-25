// The read contract (docs/phases/P6.md §2c). Every query the app makes is in this file,
// and this is the only module that reads SUPABASE_* or calls fetch (enforced by
// tests/read-boundary.test.ts).
//
// Reads go through PostgREST against the `web` schema as anon (migration 0026). Every
// row is validated at the boundary: a shape change fails loudly instead of rendering
// wrong numbers. Route params are validated before they reach a filter string.
import { z } from "zod";

import { parseCard, type CardParse } from "./card";

const REVALIDATE_SECONDS = 600; // matches each page's `export const revalidate`
const MAX_ROWS = 1000; // Supabase PostgREST's per-request cap

export class DbError extends Error {
  override name = "DbError";
}

// ---- Row schemas (exact view columns, 0026) ----------------------------------------

const WeekRow = z.object({
  season: z.int(),
  week: z.int(),
  first_gameday: z.string().nullable(),
  last_gameday: z.string().nullable(),
  n_games: z.int(),
  n_cards: z.int(),
});

const GameRow = z.object({
  game_id: z.string(),
  season: z.int(),
  week: z.int(),
  home_team: z.string(),
  away_team: z.string(),
  gameday: z.string().nullable(), // ET calendar day, "YYYY-MM-DD"
  gametime: z.string().nullable(), // ET "HH:MM"; displayed as-is, never converted
  location: z.string().nullable(),
});

const WeekCardRow = z.object({
  game_id: z.string(),
  season: z.int(),
  week: z.int(),
  kickoff: z.string(),
  projection_status: z.int(),
  locked: z.boolean(),
  as_of: z.string(),
  projected_spread: z.number().nullable(),
  projected_total: z.number().nullable(),
  projection_status_label: z.string().nullable(),
  stability_bucket: z.enum(["low", "mid", "high"]).nullable(),
  stability_min: z.number().nullable(),
  market_status: z.int().nullable(),
  market_spread_latest: z.number().nullable(),
  market_total_latest: z.number().nullable(),
  lock_market_spread: z.number().nullable(),
  lock_market_total: z.number().nullable(),
  locked_at: z.string().nullable(),
  locks_from: z.string().nullable(),
  weather_status: z.int().nullable(),
  venue_roof_code: z.int().nullable(),
  temperature_f: z.number().nullable(),
  wind_speed_mph: z.number().nullable(),
});

const CardRow = z.object({
  game_id: z.string(),
  season: z.int(),
  week: z.int(),
  kickoff: z.string(),
  projection_status: z.int(),
  locked: z.boolean(),
  card: z.unknown(), // parsed separately by parseCard so a bad card doesn't throw
  as_of: z.string(),
  inputs_version: z.string(),
});

const SignalRow = z.object({
  season: z.int(),
  week: z.int(),
  game_id: z.string().nullable(),
  team: z.string().nullable(),
  player_id: z.string().nullable(), // always null in v1 (0026 RLS); the P7 slot
  sector: z.string(),
  signal: z.string(),
  value: z.number().nullable(),
  league_pct: z.number().nullable(), // null everywhere today; reserved Rank column
  sample_n: z.int().nullable(),
  stability: z.number().nullable(),
  as_of: z.string(),
  inputs_version: z.string(),
});

export type Week = z.infer<typeof WeekRow>;
export type Game = z.infer<typeof GameRow>;
export type WeekCard = z.infer<typeof WeekCardRow>;
export type SignalRowT = z.infer<typeof SignalRow>;
export type CardRecord = Omit<z.infer<typeof CardRow>, "card"> & { card: CardParse };

// ---- Param validation --------------------------------------------------------------

const GAME_ID = /^\d{4}_\d{2}_[A-Z]{2,3}_[A-Z]{2,3}$/;
const TEAM = /^[A-Z]{2,3}$/;

export function isGameId(s: string): boolean {
  return GAME_ID.test(s);
}

function checkSeasonWeek(season: number, week: number): void {
  if (!Number.isInteger(season) || season < 1999 || season > 2100) {
    throw new DbError(`invalid season: ${season}`);
  }
  if (!Number.isInteger(week) || week < 1 || week > 22) {
    throw new DbError(`invalid week: ${week}`);
  }
}

function checkGameId(gameId: string): void {
  if (!GAME_ID.test(gameId)) throw new DbError(`invalid game_id: ${gameId}`);
}

function checkTeam(team: string): void {
  if (!TEAM.test(team)) throw new DbError(`invalid team: ${team}`);
}

// ---- Transport ---------------------------------------------------------------------

function connection(): { base: string; headers: Record<string, string> } {
  if (typeof window !== "undefined") throw new DbError("lib/db.ts is server-only");
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_ANON_KEY;
  if (!url || !key) throw new DbError("SUPABASE_URL and SUPABASE_ANON_KEY must be set");
  const headers: Record<string, string> = { apikey: key, "Accept-Profile": "web" };
  // A legacy anon JWT also goes in Authorization. A publishable key must not.
  if (key.split(".").length === 3) headers.Authorization = `Bearer ${key}`;
  return { base: url.replace(/\/$/, ""), headers };
}

async function select<S extends z.ZodType>(
  view: string,
  params: Record<string, string>,
  schema: S,
): Promise<z.infer<S>[]> {
  const { base, headers } = connection();
  const url = new URL(`${base}/rest/v1/${view}`);
  for (const [k, v] of Object.entries({ ...params, limit: String(MAX_ROWS) })) {
    url.searchParams.set(k, v);
  }
  const res = await fetch(url, { headers, next: { revalidate: REVALIDATE_SECONDS } });
  if (!res.ok) {
    throw new DbError(`web.${view}: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
  }
  const body: unknown = await res.json();
  const parsed = z.array(schema).safeParse(body);
  if (!parsed.success) {
    const first = parsed.error.issues[0];
    throw new DbError(
      `web.${view}: unexpected row shape at ${first?.path.join(".")}: ${first?.message}`,
    );
  }
  // Never render a silently truncated list.
  if (parsed.data.length >= MAX_ROWS) {
    throw new DbError(`web.${view}: ${parsed.data.length} rows hit the ${MAX_ROWS}-row cap`);
  }
  return parsed.data;
}

// ---- The queries (Q1–Q6) -----------------------------------------------------------
// All async, so a bad param is a rejected promise, the same as a failed request.

/** Q1: every season/week in the schedule, for nav and the `/` redirect. */
export async function listWeeks(): Promise<Week[]> {
  return select("weeks", { order: "season.asc,week.asc" }, WeekRow);
}

/** Q2: a week's games, in kickoff order. */
export async function weekGames(season: number, week: number): Promise<Game[]> {
  checkSeasonWeek(season, week);
  return select(
    "games",
    {
      season: `eq.${season}`,
      week: `eq.${week}`,
      order: "gameday.asc,gametime.asc,game_id.asc",
    },
    GameRow,
  );
}

/** Q3: a week's flattened cards. Games without a card are simply absent. */
export async function weekCards(season: number, week: number): Promise<WeekCard[]> {
  checkSeasonWeek(season, week);
  return select("week_cards", { season: `eq.${season}`, week: `eq.${week}` }, WeekCardRow);
}

/** Q4: one game's identity, or null if the game_id doesn't exist. */
export async function game(gameId: string): Promise<Game | null> {
  checkGameId(gameId);
  const rows = await select("games", { game_id: `eq.${gameId}` }, GameRow);
  return rows[0] ?? null;
}

/** Q5: one game's card, or null if it has none. The card is parsed but never throws. */
export async function card(gameId: string): Promise<CardRecord | null> {
  checkGameId(gameId);
  const rows = await select("cards", { game_id: `eq.${gameId}` }, CardRow);
  const row = rows[0];
  return row ? { ...row, card: parseCard(row.card) } : null;
}

/** Q6: the signal rows behind one game: both teams' team-week rows (efficiency,
 *  availability) plus the game's own rows (market, environment). */
export async function gameSignals(
  season: number,
  week: number,
  gameId: string,
  home: string,
  away: string,
): Promise<SignalRowT[]> {
  checkSeasonWeek(season, week);
  checkGameId(gameId);
  checkTeam(home);
  checkTeam(away);
  return select(
    "signals",
    {
      season: `eq.${season}`,
      week: `eq.${week}`,
      or: `(and(game_id.is.null,team.in.(${home},${away})),game_id.eq.${gameId})`,
      order: "sector.asc,team.asc.nullsfirst,signal.asc",
    },
    SignalRow,
  );
}
