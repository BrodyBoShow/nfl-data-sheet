// Matchup card schema, card_version 1. Mirrors build_cards() in
// pipeline/synthesis/synthesizer.py (CARD_VERSION = 1). A card of any other version is
// reported as unsupported rather than guessed at (docs/phases/P6.md §7 step 3).
//
// Unknown keys are stripped, not rejected, so an additive pipeline change doesn't blank
// the page. A changed or removed field fails validation, and the page shows an error
// block instead of wrong numbers.
import { z } from "zod";

export const SUPPORTED_CARD_VERSION = 1;

const num = z.number();
const numOrNull = z.number().nullable();
const intOrNull = z.int().nullable();

// _brief(): one side of a pair_unit_signals pairing. player_id is null for team units
// now, and set for P7 player rows.
const Brief = z.object({
  signal: z.string(),
  team: z.string().nullable(),
  player_id: z.string().nullable(),
  value: numOrNull,
  stability: numOrNull,
  sample_n: intOrNull,
});

const Pairing = z.object({
  base: z.string(),
  subject: Brief,
  opponent: Brief.nullable(), // no counterpart row: never filled
});

const Term = z.object({
  role: z.enum(["offense", "opponent_defense"]),
  team: z.string(),
  signal: z.string(),
  value: num,
  week_mean: num,
  centered: num,
  stability: num,
  sample_n: intOrNull,
  beta: num,
  contribution: num,
});

const Side = z.object({
  alpha: num,
  hfa: num,
  terms: z.array(Term),
  points: num,
});

const Projection = z.object({
  model_version: z.string(),
  efficiency_fingerprint: z.string(),
  spread_home: num,
  total: num,
  pts_home: num,
  pts_away: num,
  margin_home: num,
  decomposition: z.object({ home: Side, away: Side }),
});

const Uncertainty = z.object({
  stabilities: z.record(z.string(), num),
  stability_min: num,
  stability_bucket: z.enum(["low", "mid", "high"]),
  low_stability: z.boolean(),
  outcome_noise: z.object({ margin_rms: num, total_rms: num, note: z.string() }),
  edge_validated: z.object({ spread: z.boolean(), total: z.boolean() }),
  edge_note: z.object({ spread: z.string().nullable(), total: z.string().nullable() }),
});

const SignalValue = z.object({ value: numOrNull, sample_n: intOrNull });

const Market = z.object({
  status: z.int().nullable(),
  signals: z.record(z.string(), SignalValue),
  teams: z.record(z.string(), z.record(z.string(), SignalValue)),
  in_model: z.literal(false),
});

const EdgeVsCurrent = z.object({
  spread: numOrNull,
  total: numOrNull,
  market_spread: numOrNull,
  market_total: numOrNull,
  flags: z.object({
    market_lookahead_only: z.boolean(),
    single_book_market: z.boolean(),
    spread_key_straddle: z.boolean(),
    spread_within_book_range: z.boolean(),
    total_within_book_range: z.boolean(),
  }),
  flag_notes: z.object({ spread_key_straddle: z.string().nullable() }),
  // Present in the card but never rendered (P6.md §5: "model favors ..." wording).
  summary: z.string().nullable(),
});

const EdgeAtLock = z.object({
  spread: numOrNull,
  total: numOrNull,
  market_spread: numOrNull,
  market_total: numOrNull,
});

const Lock = z.object({
  locked: z.boolean(),
  locked_at: z.string().nullable(),
  kickoff_at_lock: z.string().nullable(),
  lock_lead_hours: numOrNull,
  locks_from: z.string().nullable().optional(), // unlocked cards only
});

const Context = z.object({
  in_model: z.literal(false),
  efficiency_pairings: z.object({
    home_offense: z.array(Pairing),
    away_offense: z.array(Pairing),
  }),
  environment: z.object({
    game: z.record(z.string(), numOrNull),
    teams: z.record(z.string(), z.record(z.string(), numOrNull)),
  }),
  availability: z.record(z.string(), z.record(z.string(), numOrNull)),
});

export const CardV1 = z.object({
  card_version: z.literal(1),
  identity: z.object({
    game_id: z.string(),
    season: z.int(),
    week: z.int(),
    home_team: z.string(),
    away_team: z.string(),
    kickoff: z.string(),
    location: z.string().nullable(),
    neutral: z.boolean(),
    outside_fit_scope: z.boolean(),
  }),
  projection_status: z.int().min(1).max(5),
  projection_status_label: z.string(),
  projection: Projection.nullable(),
  uncertainty: Uncertainty.nullable(),
  market: Market,
  edge: z.object({ vs_current: EdgeVsCurrent, at_lock: EdgeAtLock.nullable() }),
  lock: Lock,
  context: Context,
});

export type Card = z.infer<typeof CardV1>;
export type CardPairing = z.infer<typeof Pairing>;
export type CardBrief = z.infer<typeof Brief>;

export type CardParse =
  | { ok: true; card: Card }
  | { ok: false; reason: "unsupported_version"; version: unknown }
  | { ok: false; reason: "invalid"; issues: string[] };

/** Parse a `card` jsonb value. Never throws; the page renders a block for each failure. */
export function parseCard(raw: unknown): CardParse {
  const version =
    typeof raw === "object" && raw !== null ? (raw as { card_version?: unknown }).card_version : undefined;
  if (version !== SUPPORTED_CARD_VERSION) {
    return { ok: false, reason: "unsupported_version", version };
  }
  const parsed = CardV1.safeParse(raw);
  if (!parsed.success) {
    return {
      ok: false,
      reason: "invalid",
      issues: parsed.error.issues.map((i) => `${i.path.join(".") || "(root)"}: ${i.message}`),
    };
  }
  return { ok: true, card: parsed.data };
}
