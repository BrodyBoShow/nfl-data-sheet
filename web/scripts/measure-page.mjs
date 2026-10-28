// Measures how much of a game page a reader sees by default (docs/phases/P11.md). Kept so
// any before/after figure is reproducible (CLAUDE.md, "A figure that reaches a public page
// is reproducible from a kept script").
//
// Reads the server-rendered HTML of a running app (or the live site) and counts tables,
// body rows and body cells, in all and outside a closed <details> (what's open on load).
// A <details open> counts as open. Static HTML only: nothing is clicked or scripted.
//
// Usage (from web/):
//   node scripts/measure-page.mjs <game_id> [<game_id> ...]
// Env: BASE_URL (default http://localhost:3107).

const base = process.env.BASE_URL ?? "http://localhost:3107";
const ids = process.argv.slice(2);
if (ids.length === 0) {
  console.error("usage: node scripts/measure-page.mjs <game_id> [...]");
  process.exit(1);
}

/** The HTML with every closed <details> block removed. Refuses nested <details>, which
 *  the non-greedy match below would cut wrong. */
function openOnly(html) {
  const blocks = [...html.matchAll(/<details(?![^>]*\bopen\b)[^>]*>[\s\S]*?<\/details>/g)];
  for (const b of blocks) {
    if (b[0].slice(1).includes("<details")) throw new Error("nested <details>: the count would be wrong");
  }
  return html.replace(/<details(?![^>]*\bopen\b)[^>]*>[\s\S]*?<\/details>/g, "");
}

function count(html) {
  const tables = [...html.matchAll(/<table[\s\S]*?<\/table>/g)].map((m) => m[0]);
  let rows = 0;
  let cells = 0;
  for (const t of tables) {
    const body = /<tbody>([\s\S]*?)<\/tbody>/.exec(t)?.[1] ?? "";
    const trs = [...body.matchAll(/<tr[\s\S]*?<\/tr>/g)].map((m) => m[0]);
    rows += trs.length;
    for (const tr of trs) cells += (tr.match(/<t[dh][\s>]/g) ?? []).length;
  }
  return { tables: tables.length, rows, cells };
}

for (const id of ids) {
  const res = await fetch(new URL(`/game/${id}`, base));
  if (!res.ok) {
    console.error(`${id}: HTTP ${res.status}`);
    process.exitCode = 1;
    continue;
  }
  const html = await res.text();
  const main = /<main[\s\S]*<\/main>/.exec(html)?.[0] ?? html;
  const all = count(main);
  const open = count(openOnly(main));
  console.log(
    `${id}  tables ${open.tables}/${all.tables}  body rows ${open.rows}/${all.rows}  ` +
      `cells ${open.cells}/${all.cells}  (open on load / in the page)`,
  );
}
