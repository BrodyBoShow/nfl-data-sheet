// Step 7 done-when check (docs/phases/P6.md §7). Against a running `next start`:
//   - every page type links to /method from the site nav, and the status line links to
//     /method#edge;
//   - /method has the #edge anchor and the report inside its frame;
//   - every signal the site can display (2026 carded weeks plus a pre-2026 week, read as
//     anon from web.signals) maps to sources (lib/sources.ts sourcesFor), and every one of
//     those sources has a section on /sources with a license.
//
// Usage (from web/, with `npx next start -p 3107` running): node scripts/check-method-sources.mjs
import { readFileSync } from "node:fs";

import { SOURCES, sourcesFor } from "../lib/sources.ts";

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
  const rows = await res.json();
  if (rows.length >= 1000) throw new Error(`${view}: ${rows.length} rows hit the cap; narrow the query`);
  return rows;
}

const results = [];
const check = (name, ok, detail = "") => results.push([name, !!ok, detail]);
async function page(path) {
  const res = await fetch(BASE + path, { redirect: "manual" });
  return { status: res.status, html: await res.text() };
}

// --- links to /method -------------------------------------------------------------------
// The 404 shape is checked at an unmatched path. A notFound() thrown inside a dynamic
// route (e.g. /2026/30) serves Next's client-rendered error shell instead: status 404,
// no server-rendered body (open item in P6.md step 7 results).
const paths = ["/2026/3", "/2020/5", "/game/2026_03_ATL_GB", "/game/2019_05_CHI_OAK", "/method", "/sources", "/no-such-page"];
const missing = [];
for (const p of paths) {
  const { html } = await page(p);
  const nav = /<nav[^>]*class="site-nav[^"]*"[^>]*>([\s\S]*?)<\/nav>/.exec(html)?.[1] ?? "";
  const status = /<p[^>]*class="status-line[^"]*"[^>]*>([\s\S]*?)<\/p>/.exec(html)?.[1] ?? "";
  if (!nav.includes('href="/method"')) missing.push(`${p} nav`);
  if (!status.includes('href="/method#edge"')) missing.push(`${p} status line`);
}
check(`every page links to /method (nav) and /method#edge (status line): ${paths.join(" ")}`, missing.length === 0, missing.join("; "));

// --- /method ----------------------------------------------------------------------------
{
  const { status, html } = await page("/method");
  const frameAt = html.indexOf("data-report-frame");
  check("/method has the #edge anchor", status === 200 && /<section[^>]*id="edge"/.test(html), `HTTP ${status}`);
  check(
    "/method renders the report inside its frame, provenance first",
    frameAt > 0 && html.indexOf("data-report-provenance", frameAt) > 0 &&
      html.indexOf("data-report-body", frameAt) > html.indexOf("data-report-provenance", frameAt),
    `frame at ${frameAt}`,
  );
}

// --- source coverage ----------------------------------------------------------------------
const pairs = new Map();
const add = (rows) => rows.forEach((r) => pairs.set(`${r.sector}/${r.signal}`, r));
for (const [season, week] of [[2026, 3], [2026, 4], [2020, 5]]) {
  const base = { season: `eq.${season}`, week: `eq.${week}`, select: "sector,signal" };
  add(await api("signals", { ...base, sector: "eq.efficiency", team: "eq.ATL" }));
  for (const sector of ["environment", "market", "availability"]) add(await api("signals", { ...base, sector: `eq.${sector}` }));
}
const { html: sourcesHtml } = await page("/sources");
const onPage = new Set([...sourcesHtml.matchAll(/data-source="([a-z_]+)"/g)].map((m) => m[1]));
const unmapped = [];
const notListed = new Set();
for (const { sector, signal } of pairs.values()) {
  const ids = sourcesFor(sector, signal);
  if (!ids) unmapped.push(`${sector}/${signal}`);
  else ids.forEach((id) => onPage.has(id) || notListed.add(id));
}
const sectors = [...new Set([...pairs.values()].map((r) => r.sector))].sort();
check(`every live signal maps to a source (${pairs.size} signals in ${sectors.join(", ")})`, unmapped.length === 0, unmapped.join(", "));
check("every mapped source has a section on /sources", notListed.size === 0, [...notListed].join(", "));
const unlicensed = SOURCES.filter((s) => {
  const sec = new RegExp(`data-source="${s.id}"[\\s\\S]*?</section>`).exec(sourcesHtml)?.[0] ?? "";
  return !sec.includes("License:");
});
check("every /sources section states its license", unlicensed.length === 0, unlicensed.map((s) => s.id).join(", "));

const width = Math.max(...results.map(([name]) => name.length));
for (const [name, ok, detail] of results) console.log(`${ok ? "PASS" : "FAIL"}  ${name.padEnd(width)}  ${ok ? "" : detail}`.trimEnd());
const allOk = results.every(([, ok]) => ok);
console.log(allOk ? "PASS" : "FAIL");
process.exit(allOk ? 0 : 1);
