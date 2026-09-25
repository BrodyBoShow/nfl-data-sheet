import { readFileSync } from "node:fs";

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Arithmetic } from "../components/game/arithmetic";
import { Availability } from "../components/game/availability";
import { Environment } from "../components/game/environment";
import { Lines } from "../components/game/lines";
import { MarketDetail } from "../components/game/market-detail";
import { PairingTable } from "../components/game/pairings";
import { LowStabilityNote } from "../components/game/signal-cells";
import { TeamSignals } from "../components/game/team-signals";
import { backtest } from "../lib/backtest";
import { parseCard, type Card, type CardPairing } from "../lib/card";
import { buildLines, edgeSpreadText, edgeTotalText, gameStatus, isLowStability, validationTag } from "../lib/game";
import { formatSignalValue, signalLabel } from "../lib/signal-labels";
import { parseTeamAliases } from "../scripts/team-aliases.mjs";
import lockedRow from "./fixtures/card_2026_03_ATL_GB.json";
import provisionalRow from "./fixtures/card_2026_03_ARI_SF.json";

const cardOf = (raw: unknown): Card => {
  const r = parseCard(raw);
  if (!r.ok) throw new Error(JSON.stringify(r));
  return r.card;
};
const locked = cardOf(lockedRow.card);
const provisional = cardOf(provisionalRow.card);
const html = (el: ReactElement) => renderToStaticMarkup(el);
const text = (h: string) =>
  h.replace(/<[^>]*>/g, " ").replace(/&#x27;/g, "'").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();

function renderAll(card: Card): string {
  const pairs = card.context.efficiency_pairings;
  const lines = buildLines(card);
  return [
    lines ? html(createElement(Lines, { v: lines })) : "",
    card.projection ? html(createElement(Arithmetic, { card, projection: card.projection })) : "",
    html(createElement(PairingTable, { rows: pairs.home_offense, subject: card.identity.home_team, opponent: card.identity.away_team })),
    html(createElement(MarketDetail, { card })),
    html(createElement(Environment, { card, kickedOff: false })),
    html(createElement(Availability, { card })),
  ].join("\n");
}

describe("lines block from real cards", () => {
  it("a locked card shows the line at lock beside the latest line, and its edge vs the lock", () => {
    const v = buildLines(locked)!;
    expect(v.marketRows.map((r) => [r.label, r.spread, r.total])).toEqual([
      ["Market at lock", "GB −6.5", "44.5"],
      ["Latest pre-kickoff market", "GB −4.5", "43.0"],
    ]);
    expect([v.model.label, v.model.spread, v.model.total]).toEqual(["Model (locked)", "GB −4.2", "40.8"]);
    expect([v.edge.spread, v.edge.total]).toEqual(["2.3 toward ATL", "model 3.7 lower"]);
    expect(v.stability).toEqual({ bucket: "low", min: "0.49" });
    expect(v.typicalMiss).toEqual({ margin: "13", total: "13" });
  });

  it("a provisional card shows one market row and its edge vs the latest line", () => {
    const v = buildLines(provisional)!;
    expect(v.marketRows.map((r) => r.label)).toEqual(["Market (latest)"]);
    expect(v.model.label).toBe("Model (provisional)");
    expect(v.locked).toBe(false);
  });

  it("a not-projected card has no lines at all", () => {
    expect(buildLines({ ...provisional, projection_status: 4 })).toBeNull();
  });

  it("edge text is distance plus direction, from the card's own edge values", () => {
    expect(edgeSpreadText(2.34971, "GB", "ATL")).toBe("2.3 toward ATL");
    expect(edgeSpreadText(-1.2, "GB", "ATL")).toBe("1.2 toward GB");
    expect(edgeSpreadText(0.04, "GB", "ATL")).toBe("none");
    expect(edgeTotalText(-3.70888)).toBe("model 3.7 lower");
    expect(edgeTotalText(2.9)).toBe("model 2.9 higher");
    expect(edgeSpreadText(null, "GB", "ATL")).toBeNull();
  });
});

describe("every edge carries its validation status", () => {
  it("the tag reads NOT VALIDATED and its tooltip gives this bucket's backtest figures", () => {
    const t = validationTag(locked, "spread");
    expect(t.validated).toBe(false);
    expect(t.evidence).toBe("Backtest 2020–25, low input stability (n 535): r −0.024 [−0.108, 0.056].");
  });

  it("drops the figures, keeping the flag, when the card's model version differs", () => {
    const other = { ...locked, projection: { ...locked.projection!, model_version: "p9-v9" } };
    expect(validationTag(other, "spread").evidence).toBeNull();
  });

  it("every rendered edge value sits in a cell with a tag", () => {
    const h = html(createElement(Lines, { v: buildLines(locked)! }));
    const edgeRow = /<tr[^>]*data-row="edge"[^>]*>([\s\S]*?)<\/tr>/.exec(h)![1]!;
    const cells = [...edgeRow.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map((m) => m[1]!);
    expect(cells).toHaveLength(2);
    for (const c of cells) expect(c).toMatch(/class="tag"[^>]*>NOT VALIDATED</);
  });

  it("never renders the card's 'model favors …' summary", () => {
    expect(locked.edge.vs_current.summary).toMatch(/favors/);
    expect(text(renderAll(locked))).not.toMatch(/favou?rs?\b/i);
  });
});

describe("model arithmetic", () => {
  it.each([["locked", locked], ["provisional", provisional]] as const)(
    "%s: rendered contributions add up to the rendered projected points (display check)",
    (_, card) => {
      const h = html(createElement(Arithmetic, { card, projection: card.projection! }));
      const sides = [...h.matchAll(/<table[^>]*data-side="(home|away)"[^>]*>([\s\S]*?)<\/table>/g)];
      expect(sides).toHaveLength(2);
      for (const [, , body] of sides) {
        const num = (s: string) => Number(s.replace("−", "-"));
        // React renders a bare data attribute as ="true".
        const contributions = [...body!.matchAll(/data-contribution="true">([^<]*)</g)].map((m) => num(m[1]!));
        const points = num(/data-points="true">([^<]*)</.exec(body!)![1]!);
        expect(contributions).toHaveLength(4); // α, home field, 2 inputs
        const sum = contributions.reduce((a, b) => a + b, 0);
        expect(Math.abs(sum - points)).toBeLessThanOrEqual(0.05 + 0.005 * contributions.length);
      }
    },
  );

  it("the β header survives uppercasing (β, not B)", () => {
    const h = html(createElement(Arithmetic, { card: locked, projection: locked.projection! }));
    expect(h).toContain('class="num keep-case">β<');
  });
});

describe("context blocks", () => {
  const pairingHtml = (rows: CardPairing[]) =>
    html(createElement(PairingTable, { rows, subject: "GB", opponent: "ATL" }));
  const cellsPerRow = (h: string) =>
    [...h.matchAll(/<tr><th scope="row"[\s\S]*?<\/tr>/g)].map((m) => (m[0].match(/<td/g) ?? []).length);
  const withBrief = (rows: CardPairing[], i: number, side: "subject" | "opponent", patch: object): CardPairing[] =>
    rows.map((r, j) => (j === i ? { ...r, [side]: { ...r[side]!, ...patch } } : r));

  it("no league_pct anywhere → no percentile column at all", () => {
    const h = pairingHtml(locked.context.efficiency_pairings.home_offense);
    expect(h).not.toContain("data-pct");
    expect(text(h)).not.toMatch(/\bPct\b|\bRank\b/);
    expect(new Set(cellsPerRow(h))).toEqual(new Set([6])); // value · n · stab, per side
  });

  it("one league_pct in the table → the column appears on every row, blanks as —", () => {
    const rows = withBrief(locked.context.efficiency_pairings.home_offense, 0, "subject", { league_pct: 71.4 });
    const h = pairingHtml(rows);
    expect(text(h)).toContain("Pct");
    expect(new Set(cellsPerRow(h))).toEqual(new Set([8]));
    const pcts = [...h.matchAll(/data-pct="true"[^>]*>([\s\S]*?)<\/td>/g)].map((m) => text(m[1]!));
    expect(pcts[0]).toBe("71");
    expect(pcts.slice(1).every((p) => p === "—")).toBe(true);
  });

  it("a card brief's league_pct survives parsing (so the column can come back)", () => {
    const raw = structuredClone(lockedRow.card) as { context: { efficiency_pairings: { home_offense: { subject: object }[] } } };
    raw.context.efficiency_pairings.home_offense[0]!.subject = { ...raw.context.efficiency_pairings.home_offense[0]!.subject, league_pct: 12 };
    expect(cardOf(raw).context.efficiency_pairings.home_offense[0]!.subject.league_pct).toBe(12);
  });

  it("values below the stability floor are dimmed with a reason; n and stab are not", () => {
    const floor = backtest.stability_floor;
    let rows = withBrief(locked.context.efficiency_pairings.home_offense, 0, "subject", { stability: 0.02, sample_n: 1 });
    rows = withBrief(rows, 0, "opponent", { stability: floor }); // at the floor: not dimmed
    const h = pairingHtml(rows);
    const dimmed = [...h.matchAll(/<td class="num ink-3" data-low-stability="true" title="([^"]*)"/g)];
    expect(dimmed).toHaveLength(1);
    expect(dimmed[0]![1]).toContain("Stability 0.02: about 98% of this value is the league average");
    const first = /<tr><th scope="row"[\s\S]*?<\/tr>/.exec(h)![0];
    const tds = [...first.matchAll(/<td class="([^"]*)"/g)].map((m) => m[1]);
    expect(tds).toEqual(["num ink-3", "num", "num", "num", "num", "num"]);
    expect(isLowStability(floor - 1e-9)).toBe(true);
    expect(isLowStability(floor)).toBe(false);
    expect(isLowStability(null)).toBe(false);
  });

  it("the grey is defined once, only when something is grey", () => {
    expect(html(createElement(LowStabilityNote, { show: false }))).toBe("");
    expect(text(html(createElement(LowStabilityNote, { show: true })))).toContain(
      `stability below ${backtest.stability_floor.toFixed(2)}`,
    );
  });

  it("no-card team table: same column and dimming rules", () => {
    const row = (team: string, signal: string, stability: number, league_pct: number | null = null) => ({
      season: 2020, week: 5, game_id: null, team, player_id: null, sector: "efficiency", signal,
      value: 0.01, league_pct, sample_n: 3, stability, as_of: "2020-10-01T00:00:00Z", inputs_version: "x",
    });
    const rows = [row("CHI", "epa_per_play_off", 0.5), row("LV", "epa_per_play_off", 0.05), row("CHI", "epa_per_play_def", 0.6)];
    const h = html(createElement(TeamSignals, { rows, home: "LV", away: "CHI" }));
    expect(h).not.toContain("data-pct");
    expect(h.match(/data-low-stability="true"/g)).toHaveLength(1);
    expect(h).toContain("data-low-stability-note");
    const withPct = html(createElement(TeamSignals, { rows: [...rows.slice(0, 2), row("CHI", "epa_per_play_def", 0.6, 40)], home: "LV", away: "CHI" }));
    expect(withPct).toContain("data-pct");
  });

  it("weather shown → Open-Meteo credit on the block and the 10 m wind label", () => {
    const h = html(createElement(Environment, { card: locked, kickedOff: false }));
    expect(h).toContain('href="https://open-meteo.com/"');
    expect(text(h)).toContain("Outside wind (10 m est.) 3 mph");
    expect(text(h)).toContain("OpenStreetMap contributors");
  });

  it("no forecast → no weather values and no credit (nothing to credit)", () => {
    const h = html(createElement(Environment, { card: provisional, kickedOff: false }));
    expect(h).not.toContain("open-meteo.com");
    expect(text(h)).toContain("awaiting a forecast capture");
  });

  it("a stored 'awaiting' after kickoff reads as unknown, not awaiting", () => {
    const h = html(createElement(Environment, { card: provisional, kickedOff: true }));
    expect(text(h)).toContain("unknown (last computed before kickoff)");
  });

  it("a missing availability row renders —, never 0", () => {
    const h = html(createElement(Availability, { card: locked }));
    expect(locked.context.availability.ATL?.ol_cluster_count).toBeUndefined();
    expect(text(h)).toMatch(/ATL — 2/);
  });
});

describe("status", () => {
  it("locked, provisional, not locked, not projected", () => {
    const now = new Date("2026-09-24T23:00:00Z");
    expect(gameStatus(locked, now)).toEqual({ kind: "locked", at: "15:28", leadHours: "4.8" });
    expect(gameStatus(provisional, now)).toEqual({ kind: "provisional", locksFrom: "10:05" });
    expect(gameStatus(provisional, new Date("2026-09-28T00:00:00Z"))).toEqual({ kind: "not_locked" });
    expect(gameStatus({ ...provisional, projection_status: 2, projection_status_label: "awaiting efficiency signals for this week" }, now))
      .toEqual({ kind: "not_projected", label: "awaiting efficiency signals for this week" });
  });
});

describe("signal labels", () => {
  it("names the efficiency bases and formats by kind", () => {
    expect(signalLabel("epa_per_play_down1_off")).toBe("EPA/play, 1st down");
    expect(signalLabel("success_rate_pass_def")).toBe("Success rate, pass");
    expect(signalLabel("red_zone_td_rate")).toBe("Red-zone TD rate");
    expect(formatSignalValue("success_rate_off", 0.4287)).toBe("42.9%");
    expect(formatSignalValue("epa_per_play_off", -0.0104596)).toBe("−0.010");
    expect(formatSignalValue("points_per_drive_def", 1.904)).toBe("1.90");
  });
  it("falls back to the raw name for an unknown (e.g. P7) signal", () => {
    expect(signalLabel("pass_epa_per_target_off")).toBe("pass_epa_per_target_off");
    expect(formatSignalValue("pass_epa_per_target_off", 0.1234)).toBe("0.123");
  });
});

describe("team aliases come from the pipeline's own map", () => {
  const py = readFileSync(new URL("../../pipeline/core/team_aliases.py", import.meta.url), "utf8");
  it("parses the committed team_aliases.py", () => {
    expect(parseTeamAliases(py)).toEqual({ OAK: "LV", SD: "LAC", STL: "LA", WSH: "WAS" });
  });
  it("fails loudly if the dict is missing or has a line it can't read", () => {
    expect(() => parseTeamAliases(py.replace("TEAM_ABBR_ALIASES", "RENAMED"))).toThrow(/not found/);
    expect(() => parseTeamAliases(py.replace('"OAK": "LV",', 'OAK = "LV"'))).toThrow(/unrecognized/);
  });
});
