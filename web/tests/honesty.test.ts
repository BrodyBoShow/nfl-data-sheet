// The honesty suite (docs/phases/P6.md §5 "Enforced by test", step 8).
//
// 1. Vocabulary. Every app-authored UI string (string literals and JSX text in app/,
//    components/, lib/, read with the TypeScript parser) and every rendered fixture page
//    is scanned for pick and record vocabulary. The backtest report is not app-authored:
//    it renders verbatim inside its frame and is excluded from the scan, by removing its
//    exact rendered HTML from the page.
// 2. Every edge value sits in the same cell as its validation tag.
// 3. Every projection has its stability bucket in the same row (week) or table (game).
// 4. The report renders only inside its frame, under its provenance heading and preface.
//
// Pages render from the committed fixtures with lib/db.ts mocked, at a fixed clock.
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import report from "../content/backtest-report.json";
import { PlayersSection } from "../components/game/players";
import type { PlayerEff, PlayerUsage } from "../lib/db";
import {
  FAMILIES,
  HIST_TABLES,
  ROLE_GROUPS,
  TACKLE_L4_MIN_DEF_SNAPS,
  TAGS,
  formatPlayerValue,
  formatSeasons,
  headlineL4,
  parseSpan,
  type Tag,
} from "../lib/players";
import { SOURCES } from "../lib/sources";
import effFixture from "./fixtures/player_eff_2026_03_ATL_GB.json";
import usageFixture from "./fixtures/player_usage_2026_03_ATL_GB.json";

// Player queries the pages send, as (view, season, asOfWeek). Hoisted, so every copy of
// the mocked module records into the same list: the page imports lib/db through "@/",
// this file through a relative path, and vitest mocks each.
const playerQueries = vi.hoisted(() => [] as [string, number, number][]);

vi.mock("../lib/db", async (importOriginal) => {
  const real = await importOriginal<typeof import("../lib/db")>();
  const { parseCard } = await import("../lib/card");
  const weeks = (await import("./fixtures/weeks.json")).default;
  const games = (await import("./fixtures/games_2026_03.json")).default;
  const weekCards = (await import("./fixtures/week_cards_2026_03.json")).default;
  const signals = (await import("./fixtures/signals_2026_03_ATL_GB.json")).default;
  const cards = [
    (await import("./fixtures/card_2026_03_ATL_GB.json")).default,
    (await import("./fixtures/card_2026_03_ARI_SF.json")).default,
  ];
  // A game with no card: the ATL@GB identity under another id. (games is ordered by
  // game_id, so games[0] is ARI@SF; this used it until P7 step 9 needed ATL's players.)
  const atlGb = games.find((g) => g.game_id === "2026_03_ATL_GB")!;
  const noCard = { ...atlGb, game_id: "2026_03_ATL_GX", home_team: "GX" };
  // Week 1: the players section must send no query (nothing exists before a first game).
  const weekOne = { ...atlGb, game_id: "2026_01_ATL_GB", week: 1 };
  const usage = (await import("./fixtures/player_usage_2026_03_ATL_GB.json")).default;
  const eff = (await import("./fixtures/player_eff_2026_03_ATL_GB.json")).default;
  const forTeams = <R extends { team: string }>(rows: R[], home: string, away: string) =>
    rows.filter((r) => r.team === home || r.team === away);
  return {
    ...real,
    listWeeks: async () => weeks,
    weekGames: async () => games,
    weekCards: async () => weekCards,
    game: async (id: string) => [...games, noCard, weekOne].find((g) => g.game_id === id) ?? null,
    gamePlayerUsage: async (s: number, w: number, home: string, away: string) => {
      playerQueries.push(["player_usage", s, w]);
      return forTeams(usage, home, away);
    },
    gamePlayerEff: async (s: number, w: number, home: string, away: string) => {
      playerQueries.push(["player_eff", s, w]);
      return forTeams(eff, home, away);
    },
    card: async (id: string) => {
      const row = cards.find((c) => c.game_id === id);
      return row ? { ...row, card: parseCard(row.card) } : null;
    },
    gameSignals: async () => signals,
  };
});

const WEB = fileURLToPath(new URL("../", import.meta.url));

// ---- Vocabulary rules -----------------------------------------------------------------

/** Non-betting uses of otherwise banned words. Each must be in use (tested below), so the
 *  list can't quietly grow stale. Matched on lowercased, whitespace-normalized text. */
export const ALLOWED = [
  "not confidence in the result", // disclaims confidence
  "forecast confidence", // weather model coverage, not a claim about a game
  "backtest win rates", // the /method preface describing the report's tables
  "not a record of anything this site has done", // same preface
];

/** §5: "pick, play (as a bet), bet, best, lean, value, fade, hammer, sharp, lock (as a bet),
 *  confidence, recommend", and "no record, win rate, or ROI". Words with ordinary data
 *  meanings here (play, value, lock) are banned in their betting phrases only. */
export const BANNED: [string, RegExp][] = [
  ["pick", /\bpick(s|ed|ing)?\b/],
  ["bet", /\bbet(s|ting|tor|tors)?\b/],
  ["wager", /\bwager/],
  ["best", /\bbest\b/],
  ["lean", /\blean(s|ing)?\b/],
  ["fade", /\bfad(e|es|ed|ing)\b/],
  ["hammer", /\bhammer/],
  ["sharp", /\bsharps?\b/],
  ["recommend", /\brecommend/],
  ["confidence", /\bconfiden(ce|t)\b/],
  ["value (as a bet)", /\bvalue (bet|play|pick|side)s?\b|\b(good|great|strong|big|top|positive) value\b|\+ ?ev\b/],
  ["play (as a bet)", /\b(top|strong|big|great|free) plays?\b|\bplays? of the (day|week)\b/],
  ["lock (as a bet)", /\block of the\b|\block it\b|\b(top|big|strong) lock\b|\bsuper ?lock\b/],
  ["win rate", /\bwin(ning)? (rate|pct|percentage)s?\b|\bwin-loss\b/],
  ["record", /\brecords?\b/],
  ["ROI", /\broi\b|\bunits? (won|up|down)\b/],
];

const normalize = (s: string) =>
  s
    .replace(/&#x27;|&apos;|&#39;|’/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();

export function violations(text: string): string[] {
  let t = normalize(text);
  for (const a of ALLOWED) t = t.split(a).join(" ");
  return BANNED.filter(([, re]) => re.test(t)).map(([name]) => name);
}

// ---- App-authored strings -------------------------------------------------------------

function sourceFiles(): string[] {
  return ["app", "components", "lib"].flatMap((dir) =>
    readdirSync(join(WEB, dir), { recursive: true, encoding: "utf8" })
      .filter((f) => /\.tsx?$/.test(f))
      .map((f) => join(WEB, dir, f)),
  );
}

/** String literals, template text and JSX text: everything that can reach a page as text.
 *  Comments, identifiers and import paths are not UI strings. */
function uiStrings(file: string): string[] {
  const src = ts.createSourceFile(
    file,
    readFileSync(file, "utf8"),
    ts.ScriptTarget.Latest,
    true,
    file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const out: string[] = [];
  const visit = (n: ts.Node) => {
    if (ts.isImportDeclaration(n) || ts.isExportDeclaration(n)) return;
    if (
      ts.isJsxText(n) ||
      ts.isStringLiteral(n) ||
      ts.isNoSubstitutionTemplateLiteral(n) ||
      ts.isTemplateHead(n) ||
      ts.isTemplateMiddle(n) ||
      ts.isTemplateTail(n)
    ) {
      if (n.text.trim()) out.push(n.text);
    }
    ts.forEachChild(n, visit);
  };
  visit(src);
  return out;
}

const rel = (f: string) => relative(WEB, f).split(sep).join("/");

describe("vocabulary rules", () => {
  it.each([
    "Best bet: ATL",
    "our top play of the week",
    "Lock of the week",
    "a value play",
    "high confidence",
    "we lean ATL",
    "fade the public",
    "sharp money",
    "we recommend the over",
    "ATS record 12–3",
    "win rate 58%",
    "ROI +4.1%",
    "3 units won",
    "our picks",
  ])("flags %j", (s) => {
    expect(violations(s)).not.toEqual([]);
  });

  it.each([
    "EPA/play",
    "35 plays",
    "LOCKED 15:28",
    "PROVISIONAL · locks 14:15",
    "Market at lock",
    "Value",
    "about 98% of this number is the league average",
    "Stab: stability of the inputs, not confidence in the result.",
    "Forecast confidence",
    "These are backtest win rates.",
    "They are not a record of anything this site has done.",
  ])("allows %j", (s) => {
    expect(violations(s)).toEqual([]);
  });
});

describe("app-authored UI strings", () => {
  const files = sourceFiles();
  const strings = files.flatMap((f) => uiStrings(f).map((text) => ({ file: rel(f), text })));

  it("covers app/, components/ and lib/", () => {
    expect(files.length).toBeGreaterThan(20);
    expect(strings.length).toBeGreaterThan(300);
  });

  it("contain no pick or record vocabulary", () => {
    const bad = strings.flatMap(({ file, text }) => violations(text).map((v) => `${file}: [${v}] ${normalize(text)}`));
    expect(bad).toEqual([]);
  });

  it("use every allowlisted phrase (no stale exceptions)", () => {
    const all = strings.map((s) => normalize(s.text)).join("\n");
    expect(ALLOWED.filter((a) => !all.includes(a))).toEqual([]);
  });

  it("only the report frame reads the rendered report", () => {
    const readers = files.filter((f) => readFileSync(f, "utf8").includes("backtest-report.json")).map(rel);
    expect(readers).toEqual(["components/method/report-frame.tsx"]);
  });
});

// ---- Rendered pages -------------------------------------------------------------------

const render = (el: ReactElement) => renderToStaticMarkup(el);
const text = (html: string) => normalize(html.replace(/<[^>]*>/g, " "));
/** Visible text plus tooltips and labels, with the report's exact HTML removed. */
function scanned(html: string): string {
  const own = html.split(report.html).join(" ");
  const attrs = [...own.matchAll(/(?:title|aria-label)="([^"]*)"/g)].map((m) => m[1]);
  return [text(own), ...attrs].join("\n");
}
const cells = (html: string) => [...html.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map((m) => m[1]!);
/** The edge's own wording, at the start of a cell (lib/game.ts edgeSpreadText /
 *  edgeTotalText). Market moves read "moved 2.0 toward ATL", so they don't match. */
const EDGE_CELL = /^(\d+\.\d toward [a-z]{2,3}|model \d+\.\d (higher|lower))\b/;
/** Anywhere in a page's text: pages without an edge must not contain the wording at all. */
const EDGE_ANYWHERE = /\b\d+\.\d toward [a-z]{2,3}\b|\bmodel \d+\.\d (higher|lower)\b/;

const pages: Record<string, string> = {};

beforeAll(async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-09-24T23:00:00Z")); // ATL@GB locked, ARI@SF provisional
  const { default: WeekPage } = await import("../app/[season]/[week]/page");
  const { default: GamePage } = await import("../app/game/[gameId]/page");
  const { default: MethodPage } = await import("../app/method/page");
  const { default: SourcesPage } = await import("../app/sources/page");
  const { StatusLine } = await import("../components/status-line");
  pages.week = render(await WeekPage({ params: Promise.resolve({ season: "2026", week: "3" }) }));
  for (const id of ["2026_03_ATL_GB", "2026_03_ARI_SF", "2026_03_ATL_GX"]) {
    pages[id] = render(await GamePage({ params: Promise.resolve({ gameId: id }) }));
  }
  pages.method = render(MethodPage());
  pages.sources = render(SourcesPage());
  pages.status = render(StatusLine());
});
afterAll(() => {
  vi.useRealTimers();
});

describe("rendered fixture pages", () => {
  it("render every page shape", () => {
    expect(Object.keys(pages).sort()).toEqual(
      ["2026_03_ARI_SF", "2026_03_ATL_GB", "2026_03_ATL_GX", "method", "sources", "status", "week"].sort(),
    );
    expect(text(pages["2026_03_ATL_GX"]!)).toContain("no card");
  });

  it("contain no pick or record vocabulary outside the report", () => {
    const bad = Object.entries(pages).flatMap(([name, html]) => violations(scanned(html)).map((v) => `${name}: ${v}`));
    expect(bad).toEqual([]);
  });

  it("show every edge value in the same cell as its validation tag", () => {
    let edges = 0;
    for (const id of ["2026_03_ATL_GB", "2026_03_ARI_SF"]) {
      for (const c of cells(pages[id]!)) {
        if (!EDGE_CELL.test(text(c))) continue;
        edges++;
        expect(c, `${id}: ${text(c)}`).toMatch(/class="tag"[^>]*>(NOT )?VALIDATED</);
      }
    }
    expect(edges).toBeGreaterThanOrEqual(4); // spread and total on both cards
  });

  it("show no edge anywhere else", () => {
    for (const name of ["week", "2026_03_ATL_GX", "method", "sources", "status"]) {
      expect(EDGE_ANYWHERE.test(text(pages[name]!)), name).toBe(false);
    }
    expect(text(pages.week!)).not.toContain("model − market");
  });

  it("week: every projected row carries its stability bucket in the same row", () => {
    const rows = [...pages.week!.matchAll(/<tr><th scope="row"[\s\S]*?<\/tr>/g)].map((m) => cells(m[0]).map(text));
    const projected = rows.filter((r) => r[2] !== "—");
    expect(projected.length).toBeGreaterThan(0);
    for (const r of projected) expect(r[5]).toMatch(/^(low|mid|high)$/);
  });

  it("game: a projection's table carries the stability bucket", () => {
    for (const id of ["2026_03_ATL_GB", "2026_03_ARI_SF"]) {
      const table = /<table[^>]*>(?:(?!<\/table>)[\s\S])*data-row="model"[\s\S]*?<\/table>/.exec(pages[id]!)?.[0];
      expect(table, id).toBeDefined();
      const stab = /<tr data-row="stability">([\s\S]*?)<\/tr>/.exec(table!)?.[1];
      expect(text(stab ?? ""), id).toMatch(/^input stability (low|mid|high) /);
    }
  });
});

describe("the backtest report's frame", () => {
  it("renders the report exactly once, inside the frame", () => {
    const html = pages.method!;
    expect(html.split("data-report-frame").length - 1).toBe(1);
    const frameAt = html.lastIndexOf("<section", html.indexOf("data-report-frame"));
    const bodyAt = html.indexOf(report.html);
    expect(bodyAt).toBeGreaterThan(frameAt);
    expect(html.indexOf(report.html, bodyAt + 1)).toBe(-1);
    // No section closes between the frame opening and the end of the report.
    expect(html.slice(frameAt, bodyAt + report.html.length)).not.toContain("</section>");
  });

  it("opens with the provenance heading, then the preface, then the report", () => {
    const html = pages.method!;
    const frame = html.slice(html.lastIndexOf("<section", html.indexOf("data-report-frame")));
    const heading = /<h2[^>]*data-report-provenance="true"[^>]*>([\s\S]*?)<\/h2>/.exec(frame)?.[1];
    expect(text(heading ?? "")).toMatch(
      /^backtest report — generated by scripts\/backtest\.py, \d{4}-\d{2}-\d{2} \d{2}:\d{2} utc, from 1,615 out-of-sample games$/,
    );
    const preface = /<div[^>]*data-report-preface="true"[^>]*>([\s\S]*?)<\/div>/.exec(frame)?.[1] ?? "";
    const p = text(preface);
    expect(p).toContain("backtest win rates");
    expect(p).toContain("52.4% of decided games to break even (110 ÷ 210)");
    // p5-v2: one bucket clears break-even. The preface lists it, then says why that isn't
    // evidence: its distance from a coin flip in standard errors, and how many of the ten
    // buckets would clear break-even by chance.
    expect(p).toContain("1 of 10 buckets reaches it: 52.6% (spread, 2–3 pt |edge|, 151 of 287 decided)");
    expect(p).toContain("that's 2.6 points above a coin flip");
    expect(p).toContain(
      "with 287 decided games, the standard error of that share is 3.0 points, so it sits 0.9 standard " +
        "errors above 50%: far too few games to tell it apart from chance",
    );
    expect(p).toContain(
      "it's also 1 of 10 buckets examined. if the model's side won exactly half the time, about 2 of 10 " +
        "would reach 52.4% by chance, and at least one would do so about 89% of the time",
    );
    expect(p).toContain("every bucket is shown, and the correlations above are the test");
    expect(p).toContain("evidence that the edge doesn't work");
    expect(p).toContain("not a record of anything this site has done");
    const order = ["data-report-provenance", "data-report-preface", "data-report-body"].map((a) => frame.indexOf(a));
    expect(order.every((x) => x >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it("appears on no other page", () => {
    for (const [name, html] of Object.entries(pages)) {
      if (name === "method") continue;
      expect(text(html), name).not.toContain("p5 backtest report");
      expect(html, name).not.toContain("data-report-body");
    }
  });
});

// ---- Players section (P7 step 9) -------------------------------------------------------
// The ten assertions approved with the build plan (2026-10-01). They render the real
// game page from fixtures, plus the section alone where a test changes the data.

const USAGE = usageFixture as unknown as PlayerUsage[];
const EFF = effFixture as unknown as PlayerEff[];
const FAMILY_IDS = ["passing", "rushing", "receiving", "defense"];

const section = (html: string) => /<section[^>]*data-players[\s\S]*?<\/section>/.exec(html)?.[0] ?? "";
const renderSection = (usage = USAGE, eff = EFF, teamSection: string | null = "Efficiency matchups") =>
  render(PlayersSection({ usage, eff, home: "GB", away: "ATL", asOfWeek: 2, teamSection })!);
const tables = (html: string) => [...html.matchAll(/<table[^>]*data-family="([^"]+)"[\s\S]*?<\/table>/g)];
/** Each table's player order, keyed by offense team and table. */
function rowOrder(html: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const block of html.matchAll(/data-offense="([A-Z]+)"[\s\S]*?(?=data-offense=|$)/g)) {
    for (const t of tables(block[0])) {
      out[`${block[1]}/${t[1]}`] = [...t[0].matchAll(/<tr data-row="([^"]+)"/g)].map((m) => m[1]!);
    }
  }
  return out;
}
/** Column headers with their data-tags, by visible label (source tags stripped). */
function headers(html: string): { label: string; tags: string; html: string }[] {
  return [...html.matchAll(/<th scope="col"[^>]*data-tags="([^"]*)"[^>]*>([\s\S]*?)<\/th>/g)].map((m) => ({
    tags: m[1]!,
    label: text(m[2]!.replace(/<span>[\s\S]*?<\/span>/g, "")),
    html: m[0],
  }));
}

describe("players section", () => {
  it("1. renders on the game page and carries no pick or record vocabulary", () => {
    const html = section(pages["2026_03_ATL_GB"]!);
    expect(html).toContain("data-players");
    expect(tables(html).length).toBeGreaterThanOrEqual(8);
    expect(violations(scanned(html))).toEqual([]);
  });

  it("2. every PFR, NGS and FTN column names its source in its header, and /sources has each", () => {
    const html = renderSection();
    const heads = headers(html);
    const expectHead = (label: string, tags: Tag[]) => {
      const found = heads.filter((h) => h.label === normalize(label));
      expect(found.length, label).toBeGreaterThan(0);
      for (const h of found) {
        expect(h.tags, label).toBe(tags.join(","));
        for (const t of tags) expect(h.html, label).toContain(`data-source="${TAGS[t].source}"`);
      }
    };
    for (const g of ROLE_GROUPS) expectHead(g.label, g.tags);
    for (const f of FAMILIES) {
      expectHead(f.sample.label, f.sample.tags);
      expectHead(f.headline.label, f.headline.tags);
      for (const c of f.cols) expectHead(c.approx ? `${c.label} †` : c.label, c.tags);
    }
    for (const t of HIST_TABLES) for (const c of t.cols) expectHead(c.label, c.tags);
    // The providers the spec credits, checked by name, so a spec edit can't drop them.
    expect(heads.find((h) => h.label === "pressure % †")?.tags).toBe("PFR");
    expect(heads.find((h) => h.label === "time to throw (s)")?.tags).toBe("NGS");
    expect(heads.find((h) => h.label === "separation (yd)")?.tags).toBe("NGS");
    expect(heads.find((h) => h.label === "yds before contact")?.tags).toBe("PFR");
    expect(heads.find((h) => h.label === "snap %")?.tags).toBe("PFR");
    for (const c of FAMILIES.find((f) => f.id === "defense")!.cols) expect(c.tags, c.key).toContain("PFR");
    for (const t of HIST_TABLES) for (const c of t.cols) expect(c.tags, c.key).toContain("FTN");
    // Every source a header links to has its section, with a license, on /sources.
    const linked = new Set([...html.matchAll(/data-source="([a-z_]+)"/g)].map((m) => m[1]!));
    expect([...linked].sort()).toEqual(["ftn", "ngs", "pfr"]);
    for (const id of linked) {
      expect(pages.sources, id).toContain(`data-source="${id}"`);
      expect(SOURCES.find((s) => s.id === id)?.license.name, id).toBeTruthy();
    }
  });

  it("3. no player value is dimmed, and every family row has one Stab cell", () => {
    const html = renderSection();
    expect(html).not.toContain("data-low-stability");
    expect(html).not.toMatch(/<td[^>]*class="[^"]*ink-3/);
    const familyTables = tables(html).filter((t) => FAMILY_IDS.includes(t[1]!));
    expect(familyTables.length).toBeGreaterThanOrEqual(6);
    for (const t of familyTables) {
      const rows = [...t[0].matchAll(/<tr data-row=[\s\S]*?<\/tr>/g)].map((m) => m[0]);
      expect(rows.length, t[1]).toBeGreaterThan(0);
      for (const r of rows) expect(r.split("data-stab").length - 1, t[1]).toBe(1);
    }
  });

  it("4. the garbage-time statement is present wherever team efficiency is on the page", () => {
    let checked = 0;
    for (const id of ["2026_03_ATL_GB", "2026_03_ARI_SF", "2026_03_ATL_GX"]) {
      const page = pages[id]!;
      const players = section(page);
      if (!players) continue;
      const team = page.includes('id="pairings-h"')
        ? "efficiency matchups"
        : page.includes('id="teamsig-h"')
          ? "efficiency entering the week"
          : null;
      expect(team, id).not.toBeNull();
      const line = /<p[^>]*data-garbage-time[^>]*>([\s\S]*?)<\/p>/.exec(players)?.[1] ?? "";
      expect(text(line), id).toContain("player rates include garbage time");
      expect(text(line), id).toContain(`the team ratings in ${team} exclude it`);
      checked++;
    }
    expect(checked).toBe(2); // ATL@GB (card) and the no-card ATL@GX; ARI@SF has no player rows
    expect(text(renderSection(USAGE, EFF, null))).toContain("team efficiency ratings exclude it");
  });

  it("5. the snap-count statement, with the SRL credit, appears exactly once", () => {
    for (const id of ["2026_03_ATL_GB", "2026_03_ATL_GX"]) {
      const page = pages[id]!;
      expect(page.split("data-snap-statement").length - 1, id).toBe(1);
      const line = /<p[^>]*data-snap-statement[^>]*>([\s\S]*?)<\/p>/.exec(page)?.[1] ?? "";
      expect(text(line), id).toContain(
        "games and last-4 windows count games with a snap, from pro football reference snap counts (sports reference llc), via nflverse",
      );
    }
  });

  it("6. coverage history carries the CC BY-SA notice, 'not this season', and its span on every row", () => {
    const html = renderSection();
    const blocks = [...html.matchAll(/<details[^>]*data-coverage-history[\s\S]*?<\/details>/g)].map((m) => m[0]);
    expect(blocks.length).toBeGreaterThan(0);
    for (const b of blocks) {
      expect(text(/<summary[^>]*>([\s\S]*?)<\/summary>/.exec(b)?.[1] ?? "")).toMatch(/· 2025 · not this season$/);
      const notice = text(/<p[^>]*data-cc-by-sa[^>]*>([\s\S]*?)<\/p>/.exec(b)?.[1] ?? "");
      expect(notice).toContain("adapted from ftn data via nflverse");
      expect(notice).toContain("modified: this site aggregated");
      expect(notice).toContain("these values are shared under cc by-sa 4.0");
      expect(notice).toContain("without warranties");
      expect(b).toContain('href="https://creativecommons.org/licenses/by-sa/4.0/"');
      expect(b).toContain('href="https://github.com/nflverse/nflverse-data/releases/tag/pbp_participation"');
      const rows = [...b.matchAll(/<tr data-row="([^"]+)"[\s\S]*?<\/tr>/g)];
      expect(rows.length).toBeGreaterThan(0);
      for (const r of rows) {
        const span = text(/<td[^>]*data-span[^>]*>([\s\S]*?)<\/td>/.exec(r[0])?.[1] ?? "");
        expect(span, r[1]).toBe(EFF.find((e) => e.player_id === r[1])!.hist_span);
      }
    }
    // A span before FTN's first season is NFL Next Gen Stats data: withheld, not shown as FTN.
    const old = EFF.map((r) => (r.hist_span ? { ...r, hist_span: "2021-2023" } : r));
    const withheld = renderSection(USAGE, old);
    expect(withheld).not.toContain("data-span");
    expect(text(withheld)).toContain("seasons before 2023 carry a different attribution");
  });

  it("7. defense has no Pct column and says why", () => {
    const html = renderSection();
    const def = tables(html).filter((t) => t[1] === "defense");
    expect(def.length).toBe(2);
    for (const t of def) expect(headers(t[0]).map((h) => h.label)).not.toContain("pct");
    expect(html.split("data-not-ranked").length - 1).toBe(2);
    expect(text(html)).toContain("not ranked: pro football reference has no defensive row");
    // Offense families with a percentile do show it, so the absence above is the gate's.
    expect(headers(html).some((h) => h.label === "pct")).toBe(true);
  });

  it("8. row order is unchanged when percentiles are shuffled", () => {
    const base = rowOrder(renderSection());
    expect(Object.keys(base).length).toBeGreaterThanOrEqual(8);
    const pctKeys = Object.keys(EFF[0]!).filter((k) => k.endsWith("_pct")) as (keyof PlayerEff)[];
    expect(pctKeys.length).toBeGreaterThanOrEqual(3);
    // Two permutations that would reorder any table sorted or ranked by percentile: each
    // row takes the percentiles of its mirror row, or its own inverted (p → 100 − p).
    const shuffled = (invert: boolean) =>
      EFF.map((r, i) => {
        const out = { ...r } as Record<string, unknown>;
        for (const k of pctKeys) {
          const own = r[k] as number | null;
          out[k] = invert ? (own === null ? null : 100 - own) : EFF[EFF.length - 1 - i]![k];
        }
        return out as unknown as PlayerEff;
      });
    expect(rowOrder(renderSection(USAGE, shuffled(false)))).toEqual(base);
    expect(rowOrder(renderSection(USAGE, shuffled(true)))).toEqual(base);
    // The shuffle is real: the rendered percentiles changed.
    expect(renderSection(USAGE, shuffled(true))).not.toEqual(renderSection());
  });

  it("9. a missing value renders —, a sourced zero renders 0", () => {
    const row = USAGE.find((r) => r.off_snap_share_std !== null)!;
    const changed = USAGE.map((r) =>
      r.player_id === row.player_id ? { ...r, rz_target_share_std: 0, air_yards_share_std: null } : r,
    );
    const html = renderSection(changed);
    const role = tables(html)
      .filter((t) => t[1] === "role")
      .map((t) => t[0])
      .join("");
    const tr = new RegExp(`<tr data-row="${row.player_id}">[\\s\\S]*?</tr>`).exec(role)![0];
    const cell = (col: string) => text(new RegExp(`<td[^>]*data-col="${col}"[^>]*>([\\s\\S]*?)</td>`).exec(tr)![1]!);
    expect(cell("rz_target_share_std")).toBe("0.0%");
    expect(cell("air_yards_share_std")).toBe("—");
  });

  it("11. the defense last-4 tackle rate is withheld under 80 last-4 defensive snaps", () => {
    // P7 open item 15: PFR's tackles include special-teams tackles over defensive snaps.
    expect(TACKLE_L4_MIN_DEF_SNAPS).toBe(80);
    const def = FAMILIES.find((f) => f.id === "defense")!;
    // Ale Kaho's live week-3 row: 150/100 on 2 defensive snaps, all special-teams tackles.
    const kaho = EFF.find((r) => r.team === "ATL" && r.def_snaps_std !== null && r.tackles_per_snap_std !== null)!;
    const at = (n: number | null) => ({ ...kaho, player_id: `n${n}`, def_snaps_l4: n, tackles_per_snap_l4: 1.5 });
    const rows = [at(2), at(79), at(80), at(null)];
    const html = renderSection(USAGE, [...EFF, ...rows]);
    const atl = tables(html).find((t) => t[1] === "defense" && t[0].includes("ATL defense"))![0];
    const cell = (id: string, col: string) => {
      const tr = new RegExp(`<tr data-row="${id}">[\\s\\S]*?</tr>`).exec(atl)![0];
      return text(new RegExp(`<td[^>]*data-col="${col}"[^>]*>([\\s\\S]*?)</td>`).exec(tr)![1]!);
    };
    expect(cell("n2", "tackles_per_snap_l4")).toBe("—");
    expect(cell("n79", "tackles_per_snap_l4")).toBe("—");
    expect(cell("nnull", "tackles_per_snap_l4")).toBe("—");
    expect(cell("n80", "tackles_per_snap_l4")).toBe("150.0");
    // The L4 n stays shown, and the blended season value is untouched.
    expect(cell("n2", "def_snaps_l4")).toBe("2");
    expect(cell("n2", "tackles_per_snap_std")).toBe(formatPlayerValue("per100", kaho.tackles_per_snap_std)!);
    // Real fixture rows under the floor lose their last-4 value; rows at or over it keep it.
    const baseDef = tables(renderSection())
      .filter((t) => t[1] === "defense")
      .map((t) => t[0])
      .join("");
    for (const r of EFF.filter((e) => e.def_snaps_std !== null && e.tackles_per_snap_l4 !== null)) {
      const under = r.def_snaps_l4! < 80;
      expect(headlineL4(def, r), r.player_id).toBe(under ? null : r.tackles_per_snap_l4);
      const shown = formatPlayerValue("per100", r.tackles_per_snap_l4)!;
      const tr = new RegExp(`<tr data-row="${r.player_id}">[\\s\\S]*?</tr>`).exec(baseDef)![0];
      const l4 = text(/<td[^>]*data-col="tackles_per_snap_l4"[^>]*>([\s\S]*?)<\/td>/.exec(tr)![1]!);
      expect(l4, r.player_id).toBe(under ? "—" : shown);
    }
    expect(EFF.some((e) => e.def_snaps_std !== null && e.tackles_per_snap_l4 !== null && e.def_snaps_l4! < 80)).toBe(true);
    // No other family has a floor, and the block says why the cell is blank.
    for (const f of FAMILIES) expect(f.headline.l4MinSample, f.id).toBe(f.id === "defense" ? 80 : undefined);
    expect(text(html)).toContain("tackle rates need a minimum of defensive snaps to mean anything, so last 4 is blank under 80");
  });

  it("12. each coverage-history row shows its own span, gaps included, and the summary is their union", () => {
    // P7 open item 11: hist_span is each player's own seasons (format_seasons), not the table's.
    const withHist = EFF.filter((r) => r.rec_hist_n !== null && r.team === "GB");
    expect(withHist.length).toBeGreaterThanOrEqual(4);
    const spans = ["2023-2025", "2025", "2024-2025", "2023, 2025"];
    const ids = withHist.slice(0, 4).map((r) => r.player_id);
    const eff = EFF.map((r) => (ids.includes(r.player_id) ? { ...r, hist_span: spans[ids.indexOf(r.player_id)]! } : r));
    const html = renderSection(USAGE, eff);
    const block = /<details[^>]*data-coverage-history[\s\S]*?<\/details>/.exec(
      html.slice(html.indexOf('data-offense="GB"')),
    )![0];
    ids.forEach((id, i) => {
      const tr = new RegExp(`<tr data-row="${id}">[\\s\\S]*?</tr>`).exec(block)![0];
      expect(text(/<td[^>]*data-span[^>]*>([\s\S]*?)<\/td>/.exec(tr)![1]!), id).toBe(normalize(spans[i]!));
    });
    expect(text(block)).not.toContain("carry a different attribution");
    expect(text(/<summary[^>]*>([\s\S]*?)<\/summary>/.exec(block)![1]!)).toMatch(/· 2023-2025 · not this season$/);
    // The parser and formatter agree with the analyst's format, both ways.
    expect(parseSpan("2023, 2025")).toEqual([2023, 2025]);
    expect(parseSpan("2023-2025")).toEqual([2023, 2024, 2025]);
    expect(parseSpan("2025, 2023")).toBeNull();
    for (const s of spans) expect(formatSeasons(parseSpan(s)!)).toBe(s);
    // A gapped span with a season before FTN's first is still withheld, not shown as FTN.
    const old = EFF.map((r) => (r.player_id === ids[0] ? { ...r, hist_span: "2021, 2023" } : r));
    expect(text(renderSection(USAGE, old))).toContain("1 row not shown: seasons before 2023 carry a different attribution");
  });

  it("10. week 1 sends no player query and says why", async () => {
    // A week-3 page queries both views as of week 2, the week before the game.
    expect(playerQueries).toContainEqual(["player_usage", 2026, 2]);
    expect(playerQueries).toContainEqual(["player_eff", 2026, 2]);
    const before = playerQueries.length;
    const { default: GamePage } = await import("../app/game/[gameId]/page");
    const html = render(await GamePage({ params: Promise.resolve({ gameId: "2026_01_ATL_GB" }) }));
    expect(playerQueries.length).toBe(before);
    expect(html).toContain("data-players-none");
    expect(text(html)).toContain("no player rows before a team's first game of the season");
  });
});
