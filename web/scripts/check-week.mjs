// Step 5 done-when check (docs/phases/P6.md §7). Reads the week pages a running
// `next start` serves and compares every row against the `web` API, read as anon
// directly, not through lib/db.ts. So this checks the rendering end to end.
//
// Checks:
//   /2026/3: one row per scheduled game; each row's market lines equal the API's
//     exactly and its model lines are within display rounding (0.05); every row with a
//     model spread shows a LOW/MID/HIGH bucket; the header has no edge/difference column;
//     the "flipped" marker is on exactly the rows where the model and the market favor
//     different teams (computed here from the API values) and carries no number.
//   /2026/2, /2020/5: one row per game, rendered as a schedule (Matchup, Kickoff only)
//     with "No cards this week" stated exactly once.
//   / redirects to the week whose last game day is today or later (ET).
//   Unknown weeks 404.
//
// Usage (from web/, with `npx next start -p 3107` running):
//   node scripts/check-week.mjs
// Env: BASE_URL (default http://localhost:3107). Reads SUPABASE_URL /
// SUPABASE_ANON_KEY from web/.env.local.
import { readFileSync } from "node:fs";

const BASE = process.env.BASE_URL ?? "http://localhost:3107";
const env = Object.fromEntries(
  readFileSync(new URL("../.env.local", import.meta.url), "utf8")
    .split(/\r?\n/)
    .filter((l) => /^SUPABASE_(URL|ANON_KEY)=/.test(l))
    .map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]),
);
const apiHeaders = { apikey: env.SUPABASE_ANON_KEY, "Accept-Profile": "web" };
if (env.SUPABASE_ANON_KEY.split(".").length === 3) {
  apiHeaders.Authorization = `Bearer ${env.SUPABASE_ANON_KEY}`;
}

async function api(view, params) {
  const url = new URL(`${env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/${view}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, { headers: apiHeaders });
  if (!res.ok) throw new Error(`${view}: HTTP ${res.status}`);
  return res.json();
}

const decode = (s) =>
  s.replace(/<!-- -->/g, "").replace(/<[^>]*>/g, "").replace(/&amp;/g, "&").replace(/&#x27;|&apos;/g, "'").trim();

/** Rendered week table → header labels + one object per game row. */
function parseWeek(html) {
  const table = /<table class="data week-table(?: schedule)?">([\s\S]*?)<\/table>/.exec(html)?.[1];
  if (!table) return null;
  const headers = [...table.matchAll(/<th scope="col"[^>]*>([\s\S]*?)<\/th>/g)].map((m) => decode(m[1]));
  const rows = [...table.matchAll(/<tr><th scope="row"[^>]*>([\s\S]*?)<\/th>([\s\S]*?)<\/tr>/g)].map((m) => {
    const cells = [...m[2].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map((c) => decode(c[1]));
    const href = /href="\/game\/([^"]+)"/.exec(m[1])?.[1];
    return { gameId: href, matchup: decode(m[1]), cells };
  });
  return { headers, rows };
}

/** "GB −4.5" / "PK" → home-negative number, given the home team. */
function spreadValue(text, home) {
  if (text === "PK") return 0;
  const m = /^([A-Z]{2,3}) −(\d+(?:\.\d+)?)$/.exec(text);
  if (!m) return undefined;
  return m[1] === home ? -Number(m[2]) : Number(m[2]);
}

const results = [];
const check = (name, ok, detail = "") => results.push([name, !!ok, detail]);

async function page(path) {
  const res = await fetch(BASE + path, { redirect: "manual" });
  return { status: res.status, location: res.headers.get("location"), html: await res.text() };
}

// --- /2026/3 -------------------------------------------------------------------------
{
  const [weekMeta] = await api("weeks", { season: "eq.2026", week: "eq.3" });
  const games = await api("games", { season: "eq.2026", week: "eq.3" });
  const cards = await api("week_cards", { season: "eq.2026", week: "eq.3" });
  const byId = new Map(cards.map((c) => [c.game_id, c]));
  const home = new Map(games.map((g) => [g.game_id, g.home_team]));
  const p = await page("/2026/3");
  const w = p.status === 200 ? parseWeek(p.html) : null;
  check("/2026/3 renders a week table", w, `HTTP ${p.status}`);
  if (w) {
    check(
      `/2026/3 has one row per scheduled game (${weekMeta.n_games})`,
      w.rows.length === weekMeta.n_games && new Set(w.rows.map((r) => r.gameId)).size === weekMeta.n_games,
      `rows=${w.rows.length}`,
    );
    const edgeish = w.headers.filter((h) => /edge|diff|Δ|model\s*[−-]\s*market/i.test(h));
    check("/2026/3 header has no edge/difference column", edgeish.length === 0, edgeish.join(", "));
    const mismatches = [];
    const noBucket = [];
    const flipWrong = [];
    for (const r of w.rows) {
      const c = byId.get(r.gameId);
      const h = home.get(r.gameId);
      const [, mktSpread, modelCell, mktTotal, modelTotal, stab] = r.cells;
      // Option A marker: the word "flipped" before the model value, and nothing else.
      const marked = modelCell.startsWith("flipped ");
      const modelSpread = marked ? modelCell.slice("flipped ".length) : modelCell;
      if (!c) continue;
      // Expected flip, from the API values: the side each line favors (sign), with a
      // pick'em on either side (market 0, or a model line that shows as 0.0) not a flip.
      const side = (x, pk) => (x === null || pk ? 0 : Math.sign(x));
      const mSide = side(c.market_spread_latest, c.market_spread_latest === 0);
      const pSide = c.projection_status === 1
        ? side(c.projected_spread, Math.abs(c.projected_spread) < 0.05)
        : 0;
      const shouldFlip = mSide !== 0 && pSide !== 0 && mSide !== pSide;
      if (marked !== shouldFlip) flipWrong.push(`${r.gameId} marked=${marked} expected=${shouldFlip}`);
      // Anything besides the word itself (e.g. a gap value) leaves modelSpread
      // unparseable, which the line-match check below reports.
      if (spreadValue(mktSpread, h) !== c.market_spread_latest) mismatches.push(`${r.gameId} market spread ${mktSpread} vs ${c.market_spread_latest}`);
      if (Number(mktTotal) !== c.market_total_latest) mismatches.push(`${r.gameId} market total ${mktTotal} vs ${c.market_total_latest}`);
      if (c.projection_status === 1) {
        const ms = spreadValue(modelSpread, h);
        if (ms === undefined || Math.abs(ms - c.projected_spread) > 0.05) mismatches.push(`${r.gameId} model spread ${modelSpread} vs ${c.projected_spread}`);
        if (Math.abs(Number(modelTotal) - c.projected_total) > 0.05) mismatches.push(`${r.gameId} model total ${modelTotal} vs ${c.projected_total}`);
      }
      if (modelSpread !== "—" && !/^(LOW|MID|HIGH)$/.test(stab)) noBucket.push(`${r.gameId} stab="${stab}"`);
    }
    check("/2026/3 every rendered line matches the web API", mismatches.length === 0, mismatches.slice(0, 4).join("; "));
    check("/2026/3 every row with a model spread shows its bucket", noBucket.length === 0, noBucket.join("; "));
    check(
      "/2026/3 'flipped' marks exactly the rows where the favorites differ",
      flipWrong.length === 0,
      flipWrong.join("; "),
    );
  }
}

// --- weeks with no cards -------------------------------------------------------------
for (const [season, week] of [[2026, 2], [2020, 5]]) {
  const [weekMeta] = await api("weeks", { season: `eq.${season}`, week: `eq.${week}` });
  const p = await page(`/${season}/${week}`);
  const w = p.status === 200 ? parseWeek(p.html) : null;
  check(`/${season}/${week} renders`, w, `HTTP ${p.status}`);
  if (w) {
    check(`/${season}/${week} has one row per game (${weekMeta.n_games})`, w.rows.length === weekMeta.n_games, `rows=${w.rows.length}`);
    // No cards: a plain schedule (Matchup, Kickoff) with the reason stated once.
    check(
      `/${season}/${week} is a schedule with no model or market columns`,
      JSON.stringify(w.headers) === JSON.stringify(["Matchup", "Kickoff ET"]),
      `headers=${w.headers.join("|")}`,
    );
    // Count visible text only: Next repeats page content in inline <script> RSC payloads.
    const visible = decode(p.html.replace(/<script[\s\S]*?<\/script>/g, ""));
    const said = (visible.match(/No cards this week/g) ?? []).length;
    check(`/${season}/${week} says "No cards this week" exactly once`, said === 1, `found ${said}`);
  }
}

// --- redirect and 404s ---------------------------------------------------------------
{
  const weeks = await api("weeks", { order: "season.asc,week.asc" });
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date());
  const t = weeks.find((w) => w.last_gameday && w.last_gameday >= today) ?? weeks.at(-1);
  const p = await page("/");
  check(`/ redirects to /${t.season}/${t.week}`, [307, 308].includes(p.status) && p.location?.endsWith(`/${t.season}/${t.week}`), `${p.status} → ${p.location}`);
  for (const path of ["/2026/30", "/2026/19", "/1990/1", "/abc/1"]) {
    const q = await page(path);
    check(`${path} is 404`, q.status === 404, `HTTP ${q.status}`);
  }
}

const width = Math.max(...results.map(([n]) => n.length));
for (const [name, ok, detail] of results) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name.padEnd(width)}  ${ok ? "" : detail}`.trimEnd());
}
const allOk = results.every(([, ok]) => ok);
console.log(allOk ? "PASS" : "FAIL");
process.exit(allOk ? 0 : 1);
