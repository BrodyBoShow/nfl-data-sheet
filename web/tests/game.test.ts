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
import { LowStabilityNote, PctNote } from "../components/game/signal-cells";
import { TeamSignals } from "../components/game/team-signals";
import { backtest } from "../lib/backtest";
import { parseCard, type Card, type CardPairing } from "../lib/card";
import { formatOwnPct } from "../lib/format";
import {
  HEADLINE_PAIRING_BASES,
  POSITION_VOCABULARY,
  availabilityVocabularyNote,
  modelInputPairings,
  buildLines,
  edgeSpreadText,
  edgeTotalText,
  gameStatus,
  isLowStability,
  validationTag,
} from "../lib/game";
import { OFFENSE_BETTER, efficiencyRoot, formatSignalValue, goodDirection, pctThird, signalLabel } from "../lib/signal-labels";
import { SOURCES, sourcesFor } from "../lib/sources";
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
    // The fixture is a real p5-v1 lock. Figures show only for a card from the model the
    // committed backtest describes, so this one is re-labelled to that model's version.
    const current = { ...locked, projection: { ...locked.projection!, model_version: backtest.model_version } };
    const t = validationTag(current, "spread");
    expect(t.validated).toBe(false);
    expect(t.evidence).toBe("Backtest 2020–25, low input stability (n 536): r −0.027 [−0.112, 0.060].");
    expect(validationTag(locked, "spread").evidence).toBeNull(); // the p5-v1 lock itself
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
    expect(dimmed[0]![1]).toContain("Own 2%: about 98% of this number is the league average");
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
      `Own % below ${formatOwnPct(backtest.stability_floor)}`,
    );
  });

  // ---- docs/phases/P11.md, slice 1 ----

  it("the seven headline matchup rows are open, in order; the rest are collapsed and counted", () => {
    // The headline bases are a hardcoded vocabulary, so they're checked against the
    // registry (docs/signals.md's efficiency table), not the trimmed fixture card. A live
    // card pairs every registry base except the model's own (synthesizer.py,
    // exclude_bases=PRIMARY_SPEC.bases); the page adds that one from the decomposition.
    const md = readFileSync(new URL("../../docs/signals.md", import.meta.url), "utf8");
    const table = md.slice(md.indexOf("| Signal (`<name>_off`/`<name>_def`)"), md.indexOf("### Availability sector"));
    const registry = [...table.matchAll(/^\| `([a-z_0-9]+)`( … `down4`)? \|/gm)].flatMap((m) =>
      m[2] ? [1, 2, 3, 4].map((d) => m[1]!.replace(/down1$/, `down${d}`)) : [m[1]!],
    );
    expect(registry).toHaveLength(20);
    for (const b of HEADLINE_PAIRING_BASES) expect(registry, b).toContain(b);
    const synth = readFileSync(new URL("../../pipeline/synthesis/synthesizer.py", import.meta.url), "utf8");
    expect(synth).toContain("exclude_bases=PRIMARY_SPEC.bases");

    const like = locked.context.efficiency_pairings.home_offense[0]!;
    const cardRows = registry.filter((b) => b !== "epa_per_play").map((base) => ({ ...like, base }));
    const modelRows = modelInputPairings(locked, "home");
    expect(modelRows.map((r) => r.base)).toEqual(["epa_per_play"]);
    const h = html(createElement(PairingTable, { rows: cardRows, modelRows, subject: "GB", opponent: "ATL" }));
    const tableRows = (kind: string) => {
      const t = new RegExp(`<table[^>]*data-pairings="${kind}"[\\s\\S]*?</table>`).exec(h)?.[0] ?? "";
      return [...t.matchAll(/<span class="raw-name[^>]*> ([a-z0-9_]+)<\/span>/g)].map((m) => m[1]);
    };
    expect(tableRows("headline")).toEqual([...HEADLINE_PAIRING_BASES]);
    const more = registry.filter((b) => !(HEADLINE_PAIRING_BASES as readonly string[]).includes(b));
    expect(more).toHaveLength(13);
    expect(tableRows("more")).toEqual(more);
    // EPA/play is marked as the model's input, and its numbers are the decomposition's.
    expect(h.split("data-model-input").length - 1).toBe(1);
    const term = locked.projection!.decomposition.home.terms.find((t) => t.role === "offense")!;
    expect(modelRows[0]!.subject.value).toBe(term.value);
    expect(modelRows[0]!.opponent!.team).toBe("ATL");
    // "More" sits in a closed <details>, and says how many it holds.
    const details = /<details[^>]*data-pairings-more[^>]*>([\s\S]*?)<\/details>/.exec(h)!;
    expect(details[0]).not.toMatch(/<details[^>]*\bopen\b/);
    expect(text(/<summary[^>]*>([\s\S]*?)<\/summary>/.exec(details[0])![1]!)).toBe(
      `${more.length} more GB offense vs. ATL defense signals`,
    );
    expect(signalLabel("red_zone_td_rate")).toBe("Red-zone TD rate");
  });

  // ---- docs/phases/P11.md, slice 2 ----

  /** The 20 efficiency bases, from the registry (docs/signals.md), not from the code. */
  const registryBases = (): string[] => {
    const md = readFileSync(new URL("../../docs/signals.md", import.meta.url), "utf8");
    const t = md.slice(md.indexOf("| Signal (`<name>_off`/`<name>_def`)"), md.indexOf("### Availability sector"));
    return [...t.matchAll(/^\| `([a-z_0-9]+)`( … `down4`)? \|/gm)].flatMap((m) =>
      m[2] ? [1, 2, 3, 4].map((d) => m[1]!.replace(/down1$/, `down${d}`)) : [m[1]!],
    );
  };

  it("the direction table matches the registry both ways", () => {
    // A hardcoded vocabulary (CLAUDE.md): every efficiency base has a direction, and every
    // root in the table is some base's. A new base fails here instead of going untinted.
    const bases = registryBases();
    expect(bases).toHaveLength(20);
    for (const b of bases) {
      expect(efficiencyRoot(b), b).not.toBeNull();
      expect(goodDirection(`${b}_off`), b).not.toBeNull();
      expect(goodDirection(`${b}_def`), b).not.toBeNull();
    }
    const used = new Set(bases.map((b) => efficiencyRoot(b)));
    expect([...used].sort()).toEqual(Object.keys(OFFENSE_BETTER).sort());
    // The one offense metric where lower is better, and a defense flips every direction.
    expect(Object.entries(OFFENSE_BETTER).filter(([, d]) => d === "low").map(([r]) => r)).toEqual(["three_and_out_rate"]);
    expect(goodDirection("epa_per_play_off")).toBe("high");
    expect(goodDirection("epa_per_play_def")).toBe("low");
    expect(goodDirection("three_and_out_rate_def")).toBe("high");
    expect(goodDirection("success_rate_down3_def")).toBe("low");
    expect(goodDirection("market_status")).toBeNull();
  });

  it("a team Pct is shaded by third, oriented to its unit, and says so in words", () => {
    expect(pctThird("epa_per_play_off", 80)).toBe("better");
    expect(pctThird("epa_per_play_def", 80)).toBe("worse"); // allows more than most
    expect(pctThird("epa_per_play_def", 20)).toBe("better");
    expect(pctThird("three_and_out_rate_off", 80)).toBe("worse");
    expect(pctThird("epa_per_play_off", 50)).toBeNull();
    expect(pctThird("epa_per_play_off", null)).toBeNull();
    expect(pctThird("not_a_signal_off", 90)).toBeNull();

    const rows = locked.context.efficiency_pairings.home_offense.map((r) => ({
      ...r,
      subject: { ...r.subject, league_pct: 90 },
      opponent: r.opponent ? { ...r.opponent, league_pct: 90 } : null,
    }));
    const h = pairingHtml(rows);
    const cells = [...h.matchAll(/<td class="([^"]*)" data-pct="true"( data-third="([a-z]+)")?( title="([^"]*)")?/g)];
    expect(cells.length).toBe(rows.length * 2);
    // Offense at 90: better. The defense at 90 allows more than 90% of teams: worse.
    for (const [i, c] of cells.entries()) {
      const want = i % 2 === 0 ? "better" : "worse";
      expect(c[1]).toBe(`num pct-${want}`);
      expect(c[3]).toBe(want);
      expect(c[5]).toBe(`In the ${want} third of the league for this unit`);
    }
  });

  it("older cards without league_pct show no Pct column and no Pct note, with no per-week rule", () => {
    // league_pct is written from week 8 on. Nothing in the page names a week: the column
    // and the note appear exactly when some row carries a value.
    const h = pairingHtml(locked.context.efficiency_pairings.home_offense);
    expect(h).not.toContain("data-pct");
    expect(html(createElement(PctNote, { show: false }))).toBe("");
    expect(text(html(createElement(PctNote, { show: true })))).toContain(
      "Pct ranks each team among the league's teams entering this week, 0–100: higher means a higher value.",
    );
    const src = readFileSync(new URL("../app/game/[gameId]/page.tsx", import.meta.url), "utf8");
    expect(src).not.toMatch(/week\s*[<>]=?\s*8|>=\s*8/);
    // The model's input row carries its league_pct from the card's decomposition terms.
    const raw = structuredClone(lockedRow.card) as { projection: { decomposition: { home: { terms: object[] } } } };
    raw.projection.decomposition.home.terms = raw.projection.decomposition.home.terms.map((t) => ({ ...t, league_pct: 61 }));
    const m = modelInputPairings(cardOf(raw), "home");
    expect(m[0]!.subject.league_pct).toBe(61);
    expect(m[0]!.opponent!.league_pct).toBe(61);
  });

  it("the defense column says 'allowed', and stability reads as Own % everywhere a team value shows", () => {
    const h = pairingHtml(locked.context.efficiency_pairings.home_offense);
    expect(text(h)).toContain("ATL def allowed");
    expect(text(h)).not.toMatch(/\bStab\b/);
    expect(text(h)).toContain("Own %");
    const a = html(createElement(Arithmetic, { card: locked, projection: locked.projection! }));
    expect(text(a)).not.toMatch(/\bStab\b/);
    expect(text(a)).toMatch(/defense · EPA\/play allowed/);
    expect(formatOwnPct(0.4567)).toBe("46%");
  });

  it("arithmetic and market detail are collapsed, and still in the page's HTML", () => {
    const a = html(createElement(Arithmetic, { card: locked, projection: locked.projection! }));
    expect(a).toMatch(/^<details[^>]*data-arithmetic/);
    expect(a).not.toMatch(/^<details[^>]*\bopen\b/);
    expect(text(/<summary[^>]*>([\s\S]*?)<\/summary>/.exec(a)![1]!)).toMatch(/^How the model got there · GB [\d.]+, ATL [\d.]+$/);
    const m = html(createElement(MarketDetail, { card: locked }));
    expect(m).toMatch(/^<details[^>]*data-market-detail/);
    expect(m).not.toMatch(/^<details[^>]*\bopen\b/);
  });

  it("weeks 2-7: the availability note comes from the rows' own labels", () => {
    const row = (team: string, signal: string, inputs_version: string) => ({
      sector: "availability", game_id: null, team, signal, inputs_version,
    });
    const old = "snap_counts@2026-09-24 08:50:29 EDT,depth_charts@2026-09-24 08:42:14 EDT";
    const fixed = `${old},positions@${POSITION_VOCABULARY}`;
    const own = { noRows: false, ramsMissing: false };
    expect(availabilityVocabularyNote([row("GB", "ol_cluster_count", old)], ["ATL", "GB"])).toEqual(own);
    expect(availabilityVocabularyNote([row("GB", "ol_cluster_count", fixed)], ["ATL", "GB"])).toBeNull();
    // The game's own rows decide when it has any, whatever the week's say.
    expect(availabilityVocabularyNote([row("GB", "ol_cluster_count", fixed)], ["ATL", "GB"], [{ team: "KC", inputs_version: old }])).toBeNull();
    // No cluster-count row for either team: the week's rows decide (2026 week 2, NYG@LA).
    expect(availabilityVocabularyNote([], ["ATL", "GB"])).toBeNull(); // nothing to decide from
    expect(availabilityVocabularyNote([], ["ATL", "GB"], [{ team: "KC", inputs_version: old }])).toEqual({ ...own, noRows: true });
    expect(availabilityVocabularyNote([], ["ATL", "GB"], [{ team: "KC", inputs_version: fixed }])).toBeNull();
    expect(availabilityVocabularyNote([], ["NYG", "LA"], [{ team: "KC", inputs_version: old }])).toEqual({ noRows: true, ramsMissing: true });
    // Rows of other teams or other signals don't count as the game's own.
    expect(availabilityVocabularyNote([row("KC", "ol_cluster_count", old), row("GB", "snap_share_at_risk", old)], ["ATL", "GB"])).toBeNull();
    // A Rams game whose rows predate the fix and has no Rams row: the D19 sentence. A Rams
    // row present, or the rows carrying the label, and it's gone.
    expect(availabilityVocabularyNote([row("SF", "ol_cluster_count", old)], ["LA", "SF"])).toEqual({ ...own, ramsMissing: true });
    expect(availabilityVocabularyNote([row("SF", "ol_cluster_count", old), row("LA", "secondary_cluster_count", old)], ["LA", "SF"])).toEqual(own);
    expect(availabilityVocabularyNote([row("SF", "ol_cluster_count", fixed)], ["LA", "SF"])).toBeNull();
    const blank = text(
      html(createElement(Availability, { card: locked, rows: [], weekLabels: [{ team: "KC", inputs_version: old }] })),
    );
    expect(blank).toContain("so a blank here may be a team whose only flagged players were those.");

    const card = { ...locked, identity: { ...locked.identity, home_team: "SF", away_team: "LA" } };
    const shown = text(html(createElement(Availability, { card, rows: [row("SF", "ol_cluster_count", old)] as never })));
    expect(shown).toContain(
      `These O-line and secondary counts were computed before ${POSITION_VOCABULARY} and leave out offensive tackles and safeties coded SAF.`,
    );
    expect(shown).toContain("There's no Rams row: a team-code mismatch kept Rams counts from being written (since fixed).");
    expect(shown).toContain("They aren't recomputed: that would put today's depth chart into a past week.");
    expect(html(createElement(Availability, { card: locked, rows: [] }))).not.toContain("data-vocabulary-note");
  });

  it("the note's date and label are the analyst's own", () => {
    const py = readFileSync(new URL("../../pipeline/analysts/availability_impact.py", import.meta.url), "utf8");
    expect(/^_POSITION_VOCABULARY = "([0-9-]+)"$/m.exec(py)?.[1]).toBe(POSITION_VOCABULARY);
    expect(py).toContain('f"positions@{_POSITION_VOCABULARY}"');
    // The cluster signals the note reads are the ones the synthesizer puts on the card.
    const synth = readFileSync(new URL("../../pipeline/synthesis/synthesizer.py", import.meta.url), "utf8");
    expect(synth).toContain('_AVAILABILITY_CONTEXT = ("ol_cluster_count", "secondary_cluster_count")');
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

  it("snap counts → SRL credit under the arithmetic, the availability block and the no-card table", () => {
    const credited = (h: string) =>
      h.includes('data-credit="pfr"') &&
      h.includes('href="/sources#pfr"') &&
      text(h).includes("Pro Football Reference (Sports Reference LLC), via nflverse");
    expect(credited(html(createElement(Arithmetic, { card: locked, projection: locked.projection! })))).toBe(true);
    expect(credited(html(createElement(Availability, { card: locked })))).toBe(true);
    const row = {
      season: 2020, week: 5, game_id: null, team: "CHI", player_id: null, sector: "efficiency",
      signal: "epa_per_play_off", value: 0.01, league_pct: null, sample_n: 3, stability: 0.5,
      as_of: "2020-10-01T00:00:00Z", inputs_version: "x",
    };
    expect(credited(html(createElement(TeamSignals, { rows: [row], home: "LV", away: "CHI" })))).toBe(true);
  });

  it("efficiency and availability map to the PFR source, which quotes SRL's credit clause", () => {
    expect(sourcesFor("efficiency", "epa_per_play_off")).toContain("pfr");
    expect(sourcesFor("availability", "ol_cluster_count")).toContain("pfr");
    const pfr = SOURCES.find((s) => s.id === "pfr");
    expect(pfr?.terms.join(" ")).toContain("should explicitly credit SRL as the source of the data");
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
    // LAR: the Rams' code in ESPN, Sleeper and NGS (docs/phases/P8.md D19).
    expect(parseTeamAliases(py)).toEqual({ OAK: "LV", SD: "LAC", STL: "LA", WSH: "WAS", LAR: "LA" });
  });
  it("fails loudly if the dict is missing or has a line it can't read", () => {
    expect(() => parseTeamAliases(py.replace("TEAM_ABBR_ALIASES", "RENAMED"))).toThrow(/not found/);
    expect(() => parseTeamAliases(py.replace('"OAK": "LV",', 'OAK = "LV"'))).toThrow(/unrecognized/);
  });
});
