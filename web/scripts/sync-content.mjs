// prebuild / predev / pretypecheck: copy the two build-time artifacts into web/content/
// (gitignored) and derive content/backtest-summary.json from them
// (docs/phases/P6.md §3, step 4). A missing file or a report that doesn't parse exits
// non-zero, which fails the build.
//
// These are repo artifacts of L3's own synthesis step, not a data source. The app
// never reads them at runtime.
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { buildSummary } from "./backtest-content.mjs";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const repo = join(web, "..");
const SOURCES = {
  report: "docs/backtest_report.md",
  coefficients: "pipeline/synthesis/model_coefficients.json",
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
try {
  summary = buildSummary(reportMd, JSON.parse(coefficientsText));
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
console.log(
  `sync-content: ${SOURCES.report} + ${SOURCES.coefficients} → content/ ` +
    `(margin MAE ${summary.margin_mae.model} vs ${summary.margin_mae.close}, ` +
    `spread edge r ${summary.spread_edge_corr.r})`,
);
