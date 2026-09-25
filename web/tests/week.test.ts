import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { WeekTable } from "../components/week-table";
import type { Game, WeekCard } from "../lib/db";
import { formatGameday, formatLine, formatSpread } from "../lib/format";
import { buildWeek, buildWeekRow } from "../lib/week";
import gamesFixture from "./fixtures/games_2026_03.json";
import cardsFixture from "./fixtures/week_cards_2026_03.json";

const games = gamesFixture as Game[];
const cards = cardsFixture as WeekCard[];
const FIT = [2019, 2020, 2021, 2022, 2023, 2024, 2025];
const ctx = (iso: string) => ({ now: new Date(iso), today: iso.slice(0, 10), fitSeasons: FIT });
const tnf = games.find((g) => g.game_id === "2026_03_ATL_GB")!;
const sf = games.find((g) => g.game_id === "2026_03_ARI_SF")!;
const card = (id: string) => cards.find((c) => c.game_id === id)!;
const game = (over: Partial<Game>): Game => ({ ...tnf, ...over });

describe("formatters", () => {
  it("labels spreads by the favored team and keeps market lines exact", () => {
    expect(formatSpread(-4.5, "GB", "ATL", "market")).toBe("GB −4.5");
    expect(formatSpread(2.5, "CLE", "CAR", "market")).toBe("CAR −2.5");
    expect(formatSpread(2.25, "CLE", "CAR", "market")).toBe("CAR −2.25");
    expect(formatSpread(-4.15029, "GB", "ATL", "model")).toBe("GB −4.2");
    expect(formatSpread(0.04, "GB", "ATL", "model")).toBe("PK");
    expect(formatLine(48)).toBe("48.0");
    expect(formatLine(42.75)).toBe("42.75");
  });
  it("shows game days as weekday + month-day", () => {
    expect(formatGameday("2026-09-24")).toBe("THU 09-24");
    expect(formatGameday("2026-09-27")).toBe("SUN 09-27");
  });
});

describe("week rows from real Q2/Q3 fixtures", () => {
  it("a locked card shows the lock time (ET), both lines, and its bucket", () => {
    const r = buildWeekRow(tnf, card("2026_03_ATL_GB"), ctx("2026-09-24T23:00:00Z"));
    expect(r.status).toEqual({ kind: "locked", at: "15:28" });
    expect(r.market).toEqual({ spreadHome: -4.5, total: 43 });
    expect(r.projection?.bucket).toBe("low");
    expect(r.projection?.spreadHome).toBeCloseTo(-4.15, 2);
  });

  it("an unlocked card before kickoff is provisional, with its lock-window time", () => {
    const r = buildWeekRow(sf, card("2026_03_ARI_SF"), ctx("2026-09-24T23:00:00Z"));
    expect(r.status).toEqual({ kind: "provisional", locksFrom: "10:05" });
  });

  it("an unlocked card after kickoff is flagged, never shown as provisional", () => {
    const r = buildWeekRow(sf, card("2026_03_ARI_SF"), ctx("2026-09-28T02:00:00Z"));
    expect(r.status).toEqual({ kind: "not_locked" });
  });

  it("groups by ET game day in schedule order", () => {
    const days = buildWeek([tnf, sf], cards, ctx("2026-09-24T23:00:00Z"));
    expect(days.map((d) => [d.label, d.rows.map((r) => r.gameId)])).toEqual([
      ["THU 09-24", ["2026_03_ATL_GB"]],
      ["SUN 09-27", ["2026_03_ARI_SF"]],
    ]);
  });
});

describe("a projection never appears without its bucket, and no row carries an edge", () => {
  it("status 1 with a null bucket is shown as not projected", () => {
    const c = { ...card("2026_03_ARI_SF"), stability_bucket: null };
    const r = buildWeekRow(sf, c, ctx("2026-09-24T23:00:00Z"));
    expect(r.projection).toBeNull();
    expect(r.status).toMatchObject({ kind: "not_projected" });
  });

  it("a not-projected status shows its label and no numbers", () => {
    const c = { ...card("2026_03_ARI_SF"), projection_status: 4, projection_status_label: "model stale" };
    const r = buildWeekRow(sf, c, ctx("2026-09-24T23:00:00Z"));
    expect(r.projection).toBeNull();
    expect(r.status).toEqual({ kind: "not_projected", label: "model stale" });
  });

  it("row objects have no edge field anywhere", () => {
    const rows = buildWeek(games, cards, ctx("2026-09-24T23:00:00Z")).flatMap((d) => d.rows);
    expect(JSON.stringify(rows)).not.toMatch(/edge/i);
  });
});

describe("favorite flip (option A): a boolean, never a size", () => {
  const now = ctx("2026-09-24T23:00:00Z");
  const withLines = (market: number | null, model: number) =>
    buildWeekRow(sf, { ...card("2026_03_ARI_SF"), market_spread_latest: market, projected_spread: model }, now);

  it.each([
    ["same favorite (real fixture ARI@SF: SF −8.5 / SF −6.1)", -8.5, -6.08764, false],
    ["PHI@CHI-shaped: market favors the away team, model the home team", 4.5, -5.35514, true],
    ["a 0.2-point flip counts exactly like a 10-point one", -0.5, 0.2, true],
    ["market pick'em is not a flip", 0, 3.1, false],
    ["model rounding to PK is not a flip", -3, 0.04, false],
  ])("%s", (_, market, model, flipped) => {
    expect(withLines(market, model).favoriteFlipped).toBe(flipped);
  });

  it("a missing market line is not a flip", () => {
    expect(withLines(null, -3).favoriteFlipped).toBe(false);
  });

  it("renders the same marker for a small and a large flip, with no number in it", () => {
    const marker = (market: number, model: number) => {
      const html = renderToStaticMarkup(
        createElement(WeekTable, {
          days: [{ gameday: "2026-09-27", label: "SUN 09-27", rows: [withLines(market, model)] }],
        }),
      );
      return /<span class="flip[^"]*"[^>]*>[^<]*<\/span>/.exec(html)?.[0];
    };
    const small = marker(-0.5, 0.2);
    const large = marker(4.5, -5.35514);
    expect(small).toBeDefined();
    expect(small).toBe(large);
    // What a reader sees, meaning the text and the tooltip, carries no number. Entities
    // are decoded first: React writes the apostrophe as &#x27;, and its digits aren't
    // content.
    const seen = /title="([^"]*)"[^>]*>([^<]*)</.exec(small ?? "");
    const decode = (s = "") => s.replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
    expect(seen?.[2]).toBe("flipped");
    expect(decode(`${seen?.[1]} ${seen?.[2]}`)).not.toMatch(/\d/);
    expect(marker(-8.5, -6.08764)).toBeUndefined();
  });
});

describe("games without a card say why", () => {
  const now = ctx("2026-09-24T23:00:00Z");
  it.each([
    ["an in-sample season", game({ game_id: "2020_05_ATL_GB", season: 2020, week: 5, gameday: "2020-10-11" }), /in-sample season/, false],
    ["a pre-synthesizer 2026 week", game({ game_id: "2026_02_ATL_GB", week: 2, gameday: "2026-09-17" }), /predates the synthesizer/, false],
    ["2018 (before the fit seasons)", game({ game_id: "2018_05_ATL_GB", season: 2018, week: 5, gameday: "2018-10-07" }), /predates the synthesizer/, false],
    ["a future week", game({ game_id: "2026_05_ATL_GB", week: 5, gameday: "2026-10-08" }), /no card yet/, false],
    ["a played post-go-live game (unexpected)", game({ game_id: "2026_03_X_Y", week: 3, gameday: "2026-09-21" }), /^no card$/, true],
  ])("%s", (_, g, reason, warn) => {
    const r = buildWeekRow(g, undefined, now);
    expect(r.status.kind).toBe("no_card");
    if (r.status.kind !== "no_card") return;
    expect(r.status.reason).toMatch(reason);
    expect(r.status.warn).toBe(warn);
  });
});

describe("rendered week table", () => {
  const html = renderToStaticMarkup(
    createElement(WeekTable, { days: buildWeek(games, cards, ctx("2026-09-24T23:00:00Z")) }),
  );
  const headers = [...html.matchAll(/<th scope="col"[^>]*>([^<]*)<\/th>/g)].map((m) => m[1]);

  it("has the §4 columns and no edge / difference column", () => {
    expect(headers).toEqual([
      "Matchup", "Kickoff ET", "Market spread", "Model spread", "Market total", "Model total",
      "Stab", "Status", "Venue",
    ]);
    expect(headers.join(" ")).not.toMatch(/edge|diff|Δ|model\s*[−-]\s*market/i);
  });

  it("every row with a model spread shows its bucket", () => {
    const rows = html.split("<tr>").slice(1).filter((r) => r.includes('scope="row"'));
    expect(rows).toHaveLength(2);
    for (const r of rows) {
      const cells = [...r.matchAll(/<td[^>]*>(.*?)<\/td>/g)].map((m) => m[1]);
      const modelSpread = cells[2];
      const stab = cells[5] ?? "";
      if (modelSpread && !modelSpread.includes("—")) expect(stab).toMatch(/LOW|MID|HIGH/);
    }
  });

  it("carries the Open-Meteo credit when weather is shown", () => {
    expect(html).toContain("outside wind 3 mph");
    expect(html).toContain('href="https://open-meteo.com/"');
  });
});

describe("a week with no cards renders as a schedule, with the reason stated once", () => {
  const g2020 = [
    game({ game_id: "2020_05_TB_CHI", season: 2020, week: 5, gameday: "2020-10-08", home_team: "CHI", away_team: "TB" }),
    game({ game_id: "2020_05_ARI_NYJ", season: 2020, week: 5, gameday: "2020-10-11", home_team: "NYJ", away_team: "ARI" }),
  ];
  const days = buildWeek(g2020, [], ctx("2026-09-24T23:00:00Z"));
  const html = renderToStaticMarkup(createElement(WeekTable, { days }));

  it("has only Matchup and Kickoff columns, and no model or market numbers", () => {
    const headers = [...html.matchAll(/<th scope="col"[^>]*>([^<]*)<\/th>/g)].map((m) => m[1]);
    expect(headers).toEqual(["Matchup", "Kickoff ET"]);
    expect(html).not.toMatch(/Model|Market|Stab/);
  });

  it("states the reason exactly once", () => {
    expect(html.match(/in-sample season/g)).toHaveLength(1);
    expect(html).toContain("No cards this week · in-sample season");
  });

  it("a week mixing carded and uncarded games keeps the full table", () => {
    const mixed = buildWeek([tnf, { ...sf, game_id: "2026_03_X_Y" }], cards, ctx("2026-09-24T23:00:00Z"));
    const h = renderToStaticMarkup(createElement(WeekTable, { days: mixed }));
    expect(h).toContain("Model spread");
    expect(h).toContain("no card");
  });
});
