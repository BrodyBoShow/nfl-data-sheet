// Every Q1–Q6 query, run through the production lib/db.ts against the live `web` views.
// The fixtures are trimmed. This checks the full live responses against the same
// schemas: every week, every card of the current card week, and a complete Q6 set.
import { describe, expect, it } from "vitest";

import * as db from "../lib/db";

describe("live read contract (web schema, anon)", () => {
  it("Q1: every week row validates; seasons span 2019 onward", async () => {
    const weeks = await db.listWeeks();
    expect(weeks.length).toBeGreaterThan(100);
    expect(weeks.some((w) => w.season === 2019)).toBe(true);
    expect(weeks.some((w) => w.n_cards > 0)).toBe(true);
  });

  it("Q2–Q5: every game and card of every carded week validates and parses", async () => {
    const carded = (await db.listWeeks()).filter((w) => w.n_cards > 0);
    expect(carded.length).toBeGreaterThan(0);
    for (const w of carded) {
      const [games, cards] = await Promise.all([
        db.weekGames(w.season, w.week),
        db.weekCards(w.season, w.week),
      ]);
      expect(games.length).toBe(w.n_games);
      expect(cards.length).toBe(w.n_cards);
      for (const c of cards) {
        const rec = await db.card(c.game_id);
        const problem = rec?.card.ok ? null : rec?.card;
        expect(problem, `${c.game_id}: ${JSON.stringify(problem)}`).toBeNull();
        expect(await db.game(c.game_id)).not.toBeNull();
      }
    }
  });

  it("Q6: a carded game's full signal set validates and covers all four sectors", async () => {
    const [w] = (await db.listWeeks()).filter((x) => x.n_cards > 0);
    const [c] = await db.weekCards(w!.season, w!.week);
    const g = await db.game(c!.game_id);
    const rows = await db.gameSignals(g!.season, g!.week, g!.game_id, g!.home_team, g!.away_team);
    expect(new Set(rows.map((r) => r.sector))).toEqual(
      new Set(["efficiency", "availability", "market", "environment"]),
    );
    expect(rows.filter((r) => r.sector === "efficiency").length).toBe(80); // 40 × 2 teams
    expect(rows.every((r) => r.player_id === null)).toBe(true);
  });

  it("Q6 on a pre-card season returns that week's efficiency rows only", async () => {
    const [g] = await db.weekGames(2020, 5);
    const rows = await db.gameSignals(2020, 5, g!.game_id, g!.home_team, g!.away_team);
    expect(new Set(rows.map((r) => r.sector))).toEqual(new Set(["efficiency"]));
    expect(rows.length).toBe(80);
  });
});
