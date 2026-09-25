// prebuild / predev / pretypecheck / pretest: copy the two build-time artifacts into
// web/content/ (gitignored), derive content/backtest-summary.json from them
// (docs/phases/P6.md §3, step 4), render the report for /method
// (content/backtest-report.json, step 7), and extract the pipeline's team-code alias map into
// content/team-aliases.json (step 6). A missing file or anything that doesn't parse exits
// non-zero, which fails the build.
//
// These are repo artifacts of L3's own synthesis step, not a data source. The app
// never reads them at runtime.
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { Marked } from "marked";

import { buildSummary, renderReport } from "./backtest-content.mjs";
import { parseTeamAliases } from "./team-aliases.mjs";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const repo = join(web, "..");
const SOURCES = {
  report: "docs/backtest_report.md",
  coefficients: "pipeline/synthesis/model_coefficients.json",
  teamAliases: "pipeline/core/team_aliases.py",
};

function read(rel) {
  try {
    return readFileSync(join(repo, rel), "utf8");
  } catch (e) {
    console.error(`sync-content: cannot read ${rel} (${e.code ?? e.message})`);
    process.exit(1);
  }
}

const reportMd = read(SOURCES.report);
const coefficientsText = read(SOURCES.coefficients);

let summary;
let aliases;
let reportHtml;
try {
  summary = buildSummary(reportMd, JSON.parse(coefficientsText));
  aliases = parseTeamAliases(read(SOURCES.teamAliases));
  reportHtml = renderReport(reportMd, Marked);
} catch (e) {
  console.error(`sync-content: ${e.message}`);
  process.exit(1);
}

const sha = (s) => createHash("sha256").update(s).digest("hex").slice(0, 12);
summary.sources = {
  report: { path: SOURCES.report, sha256_12: sha(reportMd) },
  coefficients: { path: SOURCES.coefficients, sha256_12: sha(coefficientsText) },
};

const out = join(web, "content");
mkdirSync(out, { recursive: true });
writeFileSync(join(out, "backtest_report.md"), reportMd);
writeFileSync(join(out, "model_coefficients.json"), coefficientsText);
writeFileSync(join(out, "backtest-summary.json"), JSON.stringify(summary, null, 2) + "\n");
writeFileSync(join(out, "team-aliases.json"), JSON.stringify(aliases, null, 2) + "\n");
// The rendered report, read only by components/method/report-frame.tsx (step 8's
// honesty suite enforces that), so it can't appear on a page without its frame.
writeFileSync(join(out, "backtest-report.json"), JSON.stringify({ html: reportHtml }) + "\n");
console.log(
  `sync-content: ${SOURCES.report} + ${SOURCES.coefficients} + ${SOURCES.teamAliases} → content/ ` +
    `(margin MAE ${summary.margin_mae.model} vs ${summary.margin_mae.close}, ` +
    `spread edge r ${summary.spread_edge_corr.r}, ${Object.keys(aliases).length} team aliases)`,
);
