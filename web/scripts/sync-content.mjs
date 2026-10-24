// prebuild / predev / pretypecheck / pretest: copy the two build-time artifacts into
// web/content/ (gitignored), derive content/backtest-summary.json from them
// (docs/phases/P6.md §3, step 4), render the report for /method
// (content/backtest-report.json, step 7), extract the pipeline's team-code alias map into
// content/team-aliases.json (step 6), and extract Player efficiency's percentile minimums
// into content/pct-minimums.json (each Pct cell names its own population). A missing file
// or anything that doesn't parse exits non-zero, which fails the build.
//
// These are repo artifacts of L3's own synthesis step, not a data source. The app
// never reads them at runtime.
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { Marked } from "marked";

import { buildSummary, renderReport } from "./backtest-content.mjs";
import { parsePctMinimums } from "./pct-minimums.mjs";
import { parseTeamAliases } from "./team-aliases.mjs";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const repo = join(web, "..");
const SOURCES = {
  report: "docs/backtest_report.md",
  coefficients: "pipeline/synthesis/model_coefficients.json",
  teamAliases: "pipeline/core/team_aliases.py",
  pctMinimums: "pipeline/analysts/player_efficiency.py",
  // The superseded p5-v2 artifacts, for /method's earlier-beside-new figures
  // (docs/backtest_archive/p5-v2/README.md; docs/phases/P7.md, open item 22).
  previousReport: "docs/backtest_archive/p5-v2/backtest_report.md",
  previousCoefficients: "docs/backtest_archive/p5-v2/model_coefficients.json",
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

const previousReportMd = read(SOURCES.previousReport);
const previousCoefficientsText = read(SOURCES.previousCoefficients);

let summary;
let previous;
let aliases;
let minimums;
let reportHtml;
try {
  summary = buildSummary(reportMd, JSON.parse(coefficientsText));
  previous = buildSummary(previousReportMd, JSON.parse(previousCoefficientsText));
  aliases = parseTeamAliases(read(SOURCES.teamAliases));
  minimums = parsePctMinimums(read(SOURCES.pctMinimums));
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
previous.sources = {
  report: { path: SOURCES.previousReport, sha256_12: sha(previousReportMd) },
  coefficients: { path: SOURCES.previousCoefficients, sha256_12: sha(previousCoefficientsText) },
};

const out = join(web, "content");
mkdirSync(out, { recursive: true });
writeFileSync(join(out, "backtest_report.md"), reportMd);
writeFileSync(join(out, "model_coefficients.json"), coefficientsText);
writeFileSync(join(out, "backtest-summary.json"), JSON.stringify(summary, null, 2) + "\n");
writeFileSync(join(out, "backtest-summary-previous.json"), JSON.stringify(previous, null, 2) + "\n");
writeFileSync(join(out, "team-aliases.json"), JSON.stringify(aliases, null, 2) + "\n");
writeFileSync(join(out, "pct-minimums.json"), JSON.stringify(minimums, null, 2) + "\n");
// The rendered report, read only by components/method/report-frame.tsx (step 8's
// honesty suite enforces that), so it can't appear on a page without its frame.
writeFileSync(join(out, "backtest-report.json"), JSON.stringify({ html: reportHtml }) + "\n");
console.log(
  `sync-content: ${SOURCES.report} + ${SOURCES.coefficients} + ${SOURCES.teamAliases} + ` +
    `${SOURCES.pctMinimums} → content/ ` +
    `(margin MAE ${summary.margin_mae.model} vs ${summary.margin_mae.close}, ` +
    `spread edge r ${summary.spread_edge_corr.r}, ${Object.keys(aliases).length} team aliases, ` +
    `pct minimums ${JSON.stringify(minimums.default)})`,
);
