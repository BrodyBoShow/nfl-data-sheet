// One-off: capture trimmed real responses from the `web` views for the step 3 tests
// (CLAUDE.md: one trimmed real response per source; tests never call live).
//
// Fetches as anon, through the same PostgREST views the app reads, so fixture shapes
// are exactly what lib/db.ts receives. Reads SUPABASE_URL / SUPABASE_ANON_KEY from the
// repo-root .env. Writes web/tests/fixtures/*.json.
//
// Usage (from web/): node scripts/make-fixtures.mjs
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const envText = readFileSync(join(here, "..", "..", ".env"), "utf8");
const env = Object.fromEntries(
  envText
    .split(/\r?\n/)
    .filter((l) => /^[A-Z_]+=/.test(l))
    .map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]),
);
const base = env.SUPABASE_URL?.replace(/\/$/, "");
const key = env.SUPABASE_ANON_KEY;
if (!base || !key) throw new Error("SUPABASE_URL and SUPABASE_ANON_KEY must be set in .env");

const headers = { apikey: key, "Accept-Profile": "web" };
if (key.split(".").length === 3) headers.Authorization = `Bearer ${key}`;

async function get(view, params) {
  const url = new URL(`${base}/rest/v1/${view}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, { headers });
  if (!res.ok) throw new Error(`${view}: ${res.status} ${await res.text()}`);
  return res.json();
}

const out = join(here, "..", "tests", "fixtures");
mkdirSync(out, { recursive: true });
const write = (name, data) => {
  writeFileSync(join(out, name), JSON.stringify(data, null, 2) + "\n");
  console.log(`wrote tests/fixtures/${name}`);
};

// Trim: keep the first 3 efficiency pairings per side. Every other block stays whole,
// since the card schema needs to see each block's real shape.
const trimCard = (row) => {
  const pairings = row.card.context.efficiency_pairings;
  for (const side of Object.keys(pairings)) pairings[side] = pairings[side].slice(0, 3);
  return row;
};

// Cards: one locked (TNF), one provisional (a Sunday game, not yet locked).
const [locked] = await get("cards", { game_id: "eq.2026_03_ATL_GB" });
const [provisional] = await get("cards", { game_id: "eq.2026_03_ARI_SF" });
if (!locked?.card?.lock?.locked) throw new Error("expected 2026_03_ATL_GB to be locked");
if (!provisional || provisional.card.lock.locked) {
  throw new Error("expected 2026_03_ARI_SF to be unlocked");
}
write("card_2026_03_ATL_GB.json", trimCard(locked));
write("card_2026_03_ARI_SF.json", trimCard(provisional));

write(
  "week_cards_2026_03.json",
  await get("week_cards", {
    season: "eq.2026", week: "eq.3", game_id: "in.(2026_03_ATL_GB,2026_03_ARI_SF)",
    order: "game_id.asc",
  }),
);
write(
  "games_2026_03.json",
  await get("games", {
    season: "eq.2026", week: "eq.3", game_id: "in.(2026_03_ATL_GB,2026_03_ARI_SF)",
    order: "game_id.asc",
  }),
);
write(
  "weeks.json",
  await get("weeks", {
    or: "(and(season.eq.2019,week.eq.1),and(season.eq.2026,week.in.(3,4)))",
    order: "season.asc,week.asc",
  }),
);
// Q6 shape for ATL@GB: a few rows per scope (team-week efficiency and availability,
// game-scope market and environment, game-team market).
const sig = (extra) =>
  get("signals", { season: "eq.2026", week: "eq.3", order: "signal.asc", limit: "2", ...extra });
write("signals_2026_03_ATL_GB.json", [
  ...(await sig({ sector: "eq.efficiency", team: "eq.GB", signal: "like.epa_per_play_*" })),
  ...(await sig({ sector: "eq.availability", team: "in.(GB,ATL)" })),
  ...(await sig({ sector: "eq.market", game_id: "eq.2026_03_ATL_GB", team: "is.null" })),
  ...(await sig({ sector: "eq.market", game_id: "eq.2026_03_ATL_GB", team: "eq.GB" })),
  ...(await sig({ sector: "eq.environment", game_id: "eq.2026_03_ATL_GB", team: "is.null" })),
]);
