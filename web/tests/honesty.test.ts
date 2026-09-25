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
  // A game with no card: the ATL@GB identity under another id.
  const noCard = { ...games[0]!, game_id: "2026_03_ATL_GX", home_team: "GX" };
  return {
    ...real,
    listWeeks: async () => weeks,
    weekGames: async () => games,
    weekCards: async () => weekCards,
    game: async (id: string) => [...games, noCard].find((g) => g.game_id === id) ?? null,
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
    expect(p).toContain("no bucket reaches it. the highest is 51.9%");
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
