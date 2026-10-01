// The read contract (docs/phases/P6.md §2c, Q1–Q8). Every query the app makes is in this file,
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

// Q7/Q8 (P7 step 9, migration 0033): exactly the columns the players section shows. The
// request's `select=` is built from these keys, so the payload is what's parsed, nothing
// more. A null name or position means players had no row for the id (never guessed).
const num = z.number().nullable();
const count = z.int().nullable();
const PlayerIdentity = {
  player_id: z.string(),
  display_name: z.string().nullable(),
  position: z.string().nullable(),
  position_group: z.string().nullable(),
  team: z.string(),
  week: z.int(),
};

const PlayerUsageRow = z.object({
  ...PlayerIdentity,
  usage_games_std: count,
  off_snap_share_std: num,
  off_snap_share_game: num,
  target_share_std: num,
  target_share_game: num,
  air_yards_share_std: num,
  air_yards_share_game: num,
  carry_share_std: num,
  carry_share_game: num,
  // Season only (user, 2026-10-01): a single-game red-zone share rests on one or two
  // touches and carries no stability, so the _game columns aren't read.
  rz_target_share_std: num,
  rz_carry_share_std: num,
  def_snap_share_std: num,
});

const PlayerEffRow = z.object({
  ...PlayerIdentity,
  pass_dropbacks_std: count,
  pass_dropbacks_l4: count,
  pass_stability: num,
  epa_per_dropback_std: num,
  epa_per_dropback_pct: num,
  epa_per_dropback_l4: num,
  dropback_success_rate_std: num,
  cpoe_std: num,
  pass_adot_std: num,
  sack_rate_std: num,
  pressure_rate_std: num,
  avg_time_to_throw_std: num,
  scramble_rate_std: num,
  rush_carries_std: count,
  rush_carries_l4: count,
  rush_stability: num,
  epa_per_carry_std: num,
  epa_per_carry_pct: num,
  epa_per_carry_l4: num,
  rush_success_rate_std: num,
  stuff_rate_std: num,
  rush_explosive_rate_std: num,
  yards_before_contact_per_carry_std: num,
  rec_targets_std: count,
  rec_targets_l4: count,
  rec_stability: num,
  epa_per_target_std: num,
  epa_per_target_pct: num,
  epa_per_target_l4: num,
  rec_success_rate_std: num,
  yards_per_target_std: num,
  rec_adot_std: num,
  yac_oe_per_reception_std: num,
  avg_separation_std: num,
  def_snaps_std: count,
  def_snaps_l4: count,
  def_stability: num,
  tackles_per_snap_std: num,
  tackles_per_snap_pct: num,
  tackles_per_snap_l4: num,
  tfl_per_snap_std: num,
  pressures_per_snap_std: num,
  sacks_per_snap_std: num,
  qb_hits_per_snap_std: num,
  targets_per_snap_std: num,
  yards_per_target_allowed_std: num,
  missed_tackle_rate_std: num,
  hist_span: z.string().nullable(),
  rec_hist_n: count,
  pass_hist_n: count,
  epa_per_target_vs_man_hist: num,
  epa_per_target_vs_zone_hist: num,
  target_rate_vs_man_hist: num,
  target_rate_vs_zone_hist: num,
  epa_per_dropback_vs_man_hist: num,
  epa_per_dropback_vs_zone_hist: num,
});

export type PlayerUsage = z.infer<typeof PlayerUsageRow>;
export type PlayerEff = z.infer<typeof PlayerEffRow>;
export const PLAYER_USAGE_COLUMNS = Object.keys(PlayerUsageRow.shape) as (keyof PlayerUsage)[];
export const PLAYER_EFF_COLUMNS = Object.keys(PlayerEffRow.shape) as (keyof PlayerEff)[];

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

/** Q7/Q8 filter: each player's latest row with week <= asOfWeek, for the two teams
 *  (docs/phases/P6.md §2b, next_week). `asOfWeek` is the week before the game. */
function asOfParams(season: number, asOfWeek: number, home: string, away: string, columns: string[]) {
  checkSeasonWeek(season, asOfWeek);
  checkTeam(home);
  checkTeam(away);
  return {
    select: columns.join(","),
    season: `eq.${season}`,
    team: `in.(${home},${away})`,
    week: `lte.${asOfWeek}`,
    or: `(next_week.is.null,next_week.gt.${asOfWeek})`,
    order: "team.asc,player_id.asc",
  };
}

/** Q7: both teams' usage rows as of `asOfWeek` (P7 step 9). */
export async function gamePlayerUsage(
  season: number,
  asOfWeek: number,
  home: string,
  away: string,
): Promise<PlayerUsage[]> {
  return select("player_usage", asOfParams(season, asOfWeek, home, away, PLAYER_USAGE_COLUMNS), PlayerUsageRow);
}

/** Q8: both teams' player-efficiency rows as of `asOfWeek` (P7 step 9). */
export async function gamePlayerEff(
  season: number,
  asOfWeek: number,
  home: string,
  away: string,
): Promise<PlayerEff[]> {
  return select("player_eff", asOfParams(season, asOfWeek, home, away, PLAYER_EFF_COLUMNS), PlayerEffRow);
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
