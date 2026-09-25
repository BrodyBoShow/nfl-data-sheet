// Step 6 done-when check (docs/phases/P6.md §7). For every game in the current card week,
// it reads the game page a running `next start` serves and compares it to that game's
// card, read as anon directly from web.cards (not through lib/db.ts).
//
// Per card page:
//   lines: each market row, the model and the edge match the card (spreads by team and
//     value, edge by distance and direction, from edge.at_lock when locked else
//     edge.vs_current);
//   every edge value sits in a cell with its validation tag; input stability shows the
//     card's bucket;
//   the arithmetic adds up (contributions ≈ projected points, per side); the percentile
//     column appears only if a brief has league_pct; exactly the values below the
//     stability floor are dimmed;
//   weather values appear only at weather_status 1, and then with the Open-Meteo credit
//     and the 10 m label;
//   the card's "model favors …" summary never appears.
// Pages without a card: a 2019 Raiders game (OAK in games, LV in signals) shows both
// teams' efficiency; a 2018 game says signals start in 2019; bad ids 404.
//
// Usage (from web/, with `npx next start -p 3107` running): node scripts/check-game.mjs
import { readFileSync } from "node:fs";

const BASE = process.env.BASE_URL ?? "http://localhost:3107";
const env = Object.fromEntries(
  readFileSync(new URL("../.env.local", import.meta.url), "utf8")
    .split(/\r?\n/)
    .filter((l) => /^SUPABASE_(URL|ANON_KEY)=/.test(l))
    .map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]),
);
const headers = { apikey: env.SUPABASE_ANON_KEY, "Accept-Profile": "web" };
if (env.SUPABASE_ANON_KEY.split(".").length === 3) headers.Authorization = `Bearer ${env.SUPABASE_ANON_KEY}`;

async function api(view, params) {
  const url = new URL(`${env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/${view}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, { headers });
  if (!res.ok) throw new Error(`${view}: HTTP ${res.status}`);
  return res.json();
}

const visible = (html) =>
  html
    .replace(/<script[\s\S]*?<\/script>/g, "")
    .replace(/<!-- -->/g, "")
    .replace(/<[^>]*>/g, " ")
    .replace(/&#x27;|&apos;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
const num = (s) => Number(String(s).replace(/−/g, "-"));

/** Cells of the lines-table row with data-row=key: [label, spread, total] as visible text. */
function linesRow(html, key) {
  const row = new RegExp(`<tr[^>]*data-row="${key}"[^>]*>([\\s\\S]*?)</tr>`).exec(html)?.[1];
  if (!row) return null;
  return [...row.matchAll(/<t[hd][^>]*>([\s\S]*?)<\/t[hd]>/g)].map((m) => ({ raw: m[1], text: visible(m[1]) }));
}

/** "GB −6.5" / "flipped CHI −5.4" / "PK" → home-negative number. */
function spreadValue(text, home) {
  const t = text.replace(/^flipped /, "");
  if (t === "PK") return 0;
  const m = /^([A-Z]{2,3}) −(\d+(?:\.\d+)?)$/.exec(t);
  if (!m) return undefined;
  return m[1] === home ? -Number(m[2]) : Number(m[2]);
}

const results = [];
const check = (name, ok, detail = "") => results.push([name, !!ok, detail]);
async function page(path) {
  const res = await fetch(BASE + path, { redirect: "manual" });
  return { status: res.status, html: await res.text() };
}

// --- every carded game in the latest carded season --------------------------------------
const carded = await api("weeks", { n_cards: "gt.0", order: "season.desc,week.asc" });
const season = carded[0].season;
const cardWeeks = carded.filter((w) => w.season === season).map((w) => w.week);
const cards = await api("cards", { season: `eq.${season}`, week: `in.(${cardWeeks.join(",")})` });
const problems = { lines: [], tags: [], stability: [], arithmetic: [], pct: [], dim: [], weather: [], summary: [], status: [] };
// The dimming floor, from the same build-time summary the app renders with.
const floor = JSON.parse(readFileSync(new URL("../content/backtest-summary.json", import.meta.url), "utf8")).stability_floor;
if (typeof floor !== "number") throw new Error("content/backtest-summary.json has no stability_floor; run npm run sync-content");
let dimTotal = 0;

for (const row of cards) {
  const c = row.card;
  const id = row.game_id;
  const { home_team: home, away_team: away } = c.identity;
  const p = await page(`/game/${id}`);
  if (p.status !== 200) {
    problems.lines.push(`${id} HTTP ${p.status}`);
    continue;
  }
  const html = p.html;
  const text = visible(html);

  // Status
  const expectStatus = c.projection_status !== 1 ? c.projection_status_label : c.lock.locked ? "LOCKED" : "PROVISIONAL";
  if (!text.includes(expectStatus)) problems.status.push(`${id} missing "${expectStatus}"`);

  if (c.projection_status === 1) {
    const lockedCard = c.lock.locked && c.edge.at_lock;
    const edge = lockedCard ? c.edge.at_lock : c.edge.vs_current;
    const expectRows = lockedCard
      ? [["market-lock", c.edge.at_lock.market_spread, c.edge.at_lock.market_total],
         ["market-latest", c.edge.vs_current.market_spread, c.edge.vs_current.market_total]]
      : [["market-latest", c.edge.vs_current.market_spread, c.edge.vs_current.market_total]];
    for (const [key, s, t] of expectRows) {
      const r = linesRow(html, key);
      if (!r) { problems.lines.push(`${id} no ${key} row`); continue; }
      if (spreadValue(r[1].text, home) !== s) problems.lines.push(`${id} ${key} spread ${r[1].text} vs ${s}`);
      if (num(r[2].text) !== t) problems.lines.push(`${id} ${key} total ${r[2].text} vs ${t}`);
    }
    const m = linesRow(html, "model");
    const ms = m && spreadValue(m[1].text, home);
    if (!m || ms === undefined || Math.abs(ms - c.projection.spread_home) > 0.05) problems.lines.push(`${id} model spread ${m?.[1].text} vs ${c.projection.spread_home}`);
    if (!m || Math.abs(num(m[2].text) - c.projection.total) > 0.05) problems.lines.push(`${id} model total ${m?.[2].text} vs ${c.projection.total}`);

    // Edge: distance and direction from the card's own edge values, each with its tag.
    const e = linesRow(html, "edge");
    const tagText = (validated) => (validated ? "VALIDATED" : "NOT VALIDATED");
    for (const [i, market] of [[1, "spread"], [2, "total"]]) {
      const cell = e?.[i];
      const val = edge[market];
      if (!cell) { problems.lines.push(`${id} no edge ${market} cell`); continue; }
      if (val === null) continue;
      const shown = cell.text.replace(/\s*(NOT )?VALIDATED\s*$/, "");
      const mag = Math.abs(val);
      const okText =
        market === "spread"
          ? Math.abs(mag) < 0.05 ? shown === "none" : (() => {
              const mm = /^(\d+\.\d) toward ([A-Z]{2,3})$/.exec(shown);
              return mm && Math.abs(Number(mm[1]) - mag) <= 0.05 && mm[2] === (val < 0 ? home : away);
            })()
          : Math.abs(mag) < 0.05 ? shown === "none" : (() => {
              const mm = /^model (\d+\.\d) (higher|lower)$/.exec(shown);
              return mm && Math.abs(Number(mm[1]) - mag) <= 0.05 && mm[2] === (val > 0 ? "higher" : "lower");
            })();
      if (!okText) problems.lines.push(`${id} edge ${market} "${shown}" vs ${val}`);
      const want = tagText(c.uncertainty.edge_validated[market]);
      if (!new RegExp(`class="tag"[^>]*>${want}<`).test(cell.raw)) problems.tags.push(`${id} edge ${market} without "${want}" tag`);
    }

    const st = linesRow(html, "stability");
    if (!st || !st[1].text.startsWith(c.uncertainty.stability_bucket.toUpperCase())) problems.stability.push(`${id} stability "${st?.[1].text}"`);

    // Arithmetic: per side, contributions add up to the shown points.
    for (const side of ["home", "away"]) {
      const body = new RegExp(`<table[^>]*data-side="${side}"[^>]*>([\\s\\S]*?)</table>`).exec(html)?.[1];
      if (!body) { problems.arithmetic.push(`${id} no ${side} table`); continue; }
      const contribs = [...body.matchAll(/data-contribution="true">([^<]*)</g)].map((x) => num(x[1]));
      const pts = num(/data-points="true">([^<]*)</.exec(body)?.[1]);
      const sum = contribs.reduce((a, b) => a + b, 0);
      if (!(contribs.length >= 3) || Math.abs(sum - pts) > 0.05 + 0.005 * contribs.length) {
        problems.arithmetic.push(`${id} ${side}: Σ ${sum.toFixed(2)} vs points ${pts}`);
      }
    }
  } else if (linesRow(html, "model")) {
    problems.lines.push(`${id} projection shown for status ${c.projection_status}`);
  }

  // Pairing tables, in page order (home offense, then away offense), rows in card order.
  // The percentile column is present iff some brief carries league_pct. A value cell is
  // dimmed iff its brief has a value and stability below the floor; the note appears iff
  // anything is dimmed.
  const pairs = c.context.efficiency_pairings;
  const hasPairs = pairs.home_offense.length > 0;
  const tables = [...html.matchAll(/<table class="data pairing-table">([\s\S]*?)<\/table>/g)].map((m) => m[1]);
  const briefsWithPct = [...pairs.home_offense, ...pairs.away_offense]
    .flatMap((r) => [r.subject, r.opponent])
    .some((b) => b?.league_pct != null);
  if (tables.some((t) => t.includes("data-pct") !== briefsWithPct)) {
    problems.pct.push(`${id} percentile column present=${tables.map((t) => t.includes("data-pct"))} vs briefs with league_pct=${briefsWithPct}`);
  }
  let expectDim = 0;
  [pairs.home_offense, pairs.away_offense].forEach((rows, ti) => {
    const trs = [...(tables[ti] ?? "").matchAll(/<tr><th scope="row"[\s\S]*?<\/tr>/g)].map((m) => m[0]);
    if (trs.length !== rows.length) { problems.dim.push(`${id} table ${ti}: ${trs.length} rows vs ${rows.length}`); return; }
    const perSide = briefsWithPct ? 4 : 3;
    rows.forEach((r, i) => {
      const tds = [...trs[i].matchAll(/<td([^>]*)>/g)].map((m) => m[1]);
      [r.subject, r.opponent].forEach((b, k) => {
        const want = b != null && b.value !== null && b.stability !== null && b.stability < floor;
        expectDim += want;
        const got = /data-low-stability="true"/.test(tds[k * perSide] ?? "") && /ink-3/.test(tds[k * perSide] ?? "");
        if (want !== got) problems.dim.push(`${id} ${r.base} ${k ? "def" : "off"} stab ${b?.stability} dimmed=${got}`);
      });
    });
  });
  const noted = html.includes("data-low-stability-note");
  if ((expectDim > 0) !== noted) problems.dim.push(`${id} note present=${noted} expected=${expectDim > 0}`);
  dimTotal += expectDim;
  if (!hasPairs && !text.includes("no efficiency signals on the card for this week yet")) {
    problems.pct.push(`${id} empty pairings without an explanation`);
  }

  const ws = c.context.environment.game.weather_status;
  const credited = html.includes('href="https://open-meteo.com/"');
  const windLabel = text.includes("Outside wind (10 m est.)");
  if (ws === 1 && !(credited && windLabel)) problems.weather.push(`${id} weather shown without credit/label`);
  if (ws !== 1 && (credited || windLabel)) problems.weather.push(`${id} weather credit/values at status ${ws}`);

  if (/\bfavou?rs?\b/i.test(text)) problems.summary.push(`${id} contains "favors"`);
}

const n = cards.length;
const byStatus = cards.reduce((a, r) => ({ ...a, [r.projection_status]: (a[r.projection_status] ?? 0) + 1 }), {});
check(
  `${season} wks ${cardWeeks.join(",")}: all ${n} carded pages render and match the card ` +
    `(projection_status counts ${JSON.stringify(byStatus)})`,
  problems.lines.length === 0,
  problems.lines.slice(0, 4).join("; "),
);
check("every edge value carries its validation tag", problems.tags.length === 0, problems.tags.slice(0, 4).join("; "));
check("input stability shows the card's bucket", problems.stability.length === 0, problems.stability.slice(0, 4).join("; "));
check("model arithmetic adds up on every page", problems.arithmetic.length === 0, problems.arithmetic.slice(0, 4).join("; "));
check("percentile column only when a brief has league_pct", problems.pct.length === 0, problems.pct.slice(0, 4).join("; "));
check(`values below stability ${floor} dimmed, no others (${dimTotal} on these cards)`, problems.dim.length === 0, problems.dim.slice(0, 4).join("; "));
check("weather only at status 1, always credited and labeled 10 m", problems.weather.length === 0, problems.weather.slice(0, 4).join("; "));
check("the card's 'model favors …' summary never appears", problems.summary.length === 0, problems.summary.slice(0, 4).join("; "));
check("status (LOCKED / PROVISIONAL / label) matches the card", problems.status.length === 0, problems.status.slice(0, 4).join("; "));

// --- pages without a card ---------------------------------------------------------------
{
  const p = await page("/game/2019_05_CHI_OAK");
  const t = visible(p.html);
  const body = /<table class="data team-signals-table">([\s\S]*?)<\/table>/.exec(p.html)?.[1] ?? "";
  const rows = [...body.matchAll(/<tr><th scope="row"[\s\S]*?<\/tr>/g)].map((m) =>
    [...m[0].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map((c) => visible(c[1])));
  // Columns: away value, n, stab, home value, n, stab (home = OAK → LV rows; no
  // percentile column while league_pct is empty).
  const homeFilled = rows.filter((r) => r[3] && r[3] !== "—").length;
  check("/game/2019_05_CHI_OAK: in-sample note, 40 efficiency rows", p.status === 200 && t.includes("in-sample season") && rows.length === 40, `HTTP ${p.status}, rows=${rows.length}`);
  check("/game/2019_05_CHI_OAK: the OAK (→ LV) side has values", homeFilled === 40, `OAK/LV filled=${homeFilled}`);
}
{
  const p = await page("/game/2018_05_ARI_SF");
  check("/game/2018_05_ARI_SF says signals start in 2019", p.status === 200 && visible(p.html).includes("Efficiency signals start in 2019"), `HTTP ${p.status}`);
}
for (const path of ["/game/2026_03_XXX_YYY", "/game/not-a-game", "/game/2026_03_ATL_GB,x"]) {
  const q = await page(path);
  check(`${path} is 404`, q.status === 404, `HTTP ${q.status}`);
}

const width = Math.max(...results.map(([name]) => name.length));
for (const [name, ok, detail] of results) console.log(`${ok ? "PASS" : "FAIL"}  ${name.padEnd(width)}  ${ok ? "" : detail}`.trimEnd());
const allOk = results.every(([, ok]) => ok);
console.log(allOk ? "PASS" : "FAIL");
process.exit(allOk ? 0 : 1);
