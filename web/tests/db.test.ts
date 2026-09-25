import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as db from "../lib/db";
import lockedCard from "./fixtures/card_2026_03_ATL_GB.json";
import games from "./fixtures/games_2026_03.json";
import signals from "./fixtures/signals_2026_03_ATL_GB.json";
import weekCards from "./fixtures/week_cards_2026_03.json";
import weeks from "./fixtures/weeks.json";

const JWT = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.sig";
const PUBLISHABLE = "sb_publishable_test";

let fetchMock: ReturnType<typeof vi.fn>;

function respond(body: unknown, status = 200) {
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
}

function lastRequest(): { url: URL; headers: Record<string, string> } {
  const [url, init] = fetchMock.mock.calls.at(-1) as [URL, RequestInit];
  return { url: new URL(url), headers: init.headers as Record<string, string> };
}

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("SUPABASE_URL", "https://example.supabase.co/");
  vi.stubEnv("SUPABASE_ANON_KEY", JWT);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("transport", () => {
  it("reads the web schema with the anon key and a row cap", async () => {
    respond(weeks);
    await db.listWeeks();
    const { url, headers } = lastRequest();
    expect(url.origin + url.pathname).toBe("https://example.supabase.co/rest/v1/weeks");
    expect(url.searchParams.get("limit")).toBe("1000");
    expect(headers["Accept-Profile"]).toBe("web");
    expect(headers.apikey).toBe(JWT);
    expect(headers.Authorization).toBe(`Bearer ${JWT}`);
  });

  it("never sends a publishable key as a Bearer token", async () => {
    vi.stubEnv("SUPABASE_ANON_KEY", PUBLISHABLE);
    respond(weeks);
    await db.listWeeks();
    const { headers } = lastRequest();
    expect(headers.apikey).toBe(PUBLISHABLE);
    expect(headers.Authorization).toBeUndefined();
  });

  it("fails without configuration", async () => {
    vi.stubEnv("SUPABASE_ANON_KEY", "");
    await expect(db.listWeeks()).rejects.toThrow(db.DbError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("surfaces HTTP errors", async () => {
    respond({ code: "42501", message: "permission denied" }, 401);
    await expect(db.listWeeks()).rejects.toThrow(/HTTP 401/);
  });

  it("rejects a changed row shape instead of rendering it", async () => {
    const { n_cards: _, ...broken } = weeks[0]!;
    respond([broken]);
    await expect(db.listWeeks()).rejects.toThrow(/unexpected row shape at 0\.n_cards/);
  });

  it("refuses a result that hit the row cap (possible silent truncation)", async () => {
    respond(Array.from({ length: 1000 }, () => weeks[0]));
    await expect(db.listWeeks()).rejects.toThrow(/row cap/);
  });
});

describe("queries against real response shapes (fixtures)", () => {
  it("Q1 listWeeks", async () => {
    respond(weeks);
    const rows = await db.listWeeks();
    expect(rows.find((w) => w.season === 2026 && w.week === 3)?.n_cards).toBe(16);
    expect(lastRequest().url.searchParams.get("order")).toBe("season.asc,week.asc");
  });

  it("Q2 weekGames filters by season and week, in kickoff order", async () => {
    respond(games);
    const rows = await db.weekGames(2026, 3);
    expect(rows.map((g) => g.game_id)).toContain("2026_03_ATL_GB");
    const p = lastRequest().url.searchParams;
    expect([p.get("season"), p.get("week"), p.get("order")]).toEqual([
      "eq.2026", "eq.3", "gameday.asc,gametime.asc,game_id.asc",
    ]);
  });

  it("Q3 weekCards", async () => {
    respond(weekCards);
    const rows = await db.weekCards(2026, 3);
    const tnf = rows.find((r) => r.game_id === "2026_03_ATL_GB");
    expect(tnf?.locked).toBe(true);
    expect(tnf?.lock_market_spread).toBe(-6.5);
    expect(tnf?.market_spread_latest).toBe(-4.5);
  });

  it("Q4 game returns null for an unknown game", async () => {
    respond([]);
    expect(await db.game("2026_03_ATL_GB")).toBeNull();
  });

  it("Q5 card parses the card and never throws on a bad one", async () => {
    respond([lockedCard]);
    const rec = await db.card("2026_03_ATL_GB");
    expect(rec?.card.ok).toBe(true);
    respond([{ ...lockedCard, card: { card_version: 9 } }]);
    const bad = await db.card("2026_03_ATL_GB");
    expect(bad?.card).toMatchObject({ ok: false, reason: "unsupported_version" });
  });

  it("Q6 gameSignals scopes to both teams' week rows plus the game's rows", async () => {
    respond(signals);
    const rows = await db.gameSignals(2026, 3, "2026_03_ATL_GB", "GB", "ATL");
    expect(new Set(rows.map((r) => r.sector))).toEqual(
      new Set(["efficiency", "availability", "market", "environment"]),
    );
    expect(rows.every((r) => r.player_id === null)).toBe(true);
    expect(lastRequest().url.searchParams.get("or")).toBe(
      "(and(game_id.is.null,team.in.(GB,ATL)),game_id.eq.2026_03_ATL_GB)",
    );
  });
});

describe("route params are validated before any request", () => {
  it.each([
    ["season", () => db.weekGames(1998, 3)],
    ["week", () => db.weekCards(2026, 23)],
    ["non-integer week", () => db.weekCards(2026, 2.5)],
    ["game_id", () => db.game("2026_03_ATL_GB,game_id.neq.x")],
    ["team", () => db.gameSignals(2026, 3, "2026_03_ATL_GB", "GB),or(x", "ATL")],
  ])("rejects a bad %s", async (_, call) => {
    await expect(call()).rejects.toThrow(db.DbError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
