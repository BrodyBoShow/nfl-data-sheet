// The backtest summary (content/backtest-summary.json, written at build time by
// scripts/sync-content.mjs) and the header status line built from it
// (docs/phases/P6.md §5, device 1).
import raw from "@/content/backtest-summary.json";

import { formatCount, formatFixed, formatInterval, formatSeasonRange } from "./format";

export interface EdgeValidation {
  n: number;
  corr: number;
  ci_low: number;
  ci_high: number;
  validated: boolean;
}

export interface BacktestSummary {
  model_version: string;
  fit_seasons: number[]; // seasons the coefficients were fit on (cards: projection_status 5)
  test_seasons: number[];
  n_games: number;
  margin_mae: { model: number; close: number };
  spread_edge_corr: { r: number; ci: number[]; n: number; validated: boolean };
  validated_buckets: string[];
  buckets: Record<string, { n_games: number; spread: EdgeValidation; total: EdgeValidation }>;
  sources: Record<string, { path: string; sha256_12: string }>;
}

// Checked against the interface at compile time: a summary-shape change fails tsc.
export const backtest: BacktestSummary = raw;

export interface StatusLine {
  scope: string; // "Backtest 2020–25, 1,615 games out of sample"
  marginModel: string; // "10.32"
  marginClose: string; // "9.76"
  edgeR: string; // "−0.03"
  edgeCi: string; // "[−0.08, 0.02]"
  edgeExact: string; // tooltip: the definition, at the report's own precision
  validation: string; // "not validated"
}

/** Every figure comes from the summary. The wording of the validation part follows
 *  the data: it can't say "not validated" if any bucket validated, or the reverse. */
export function statusLine(s: BacktestSummary): StatusLine {
  const [lo, hi] = s.spread_edge_corr.ci;
  if (lo === undefined || hi === undefined) throw new Error("edge CI needs two bounds");
  const anyValidated = s.spread_edge_corr.validated || s.validated_buckets.length > 0;
  return {
    scope: `Backtest ${formatSeasonRange(s.test_seasons)}, ${formatCount(s.n_games)} games out of sample`,
    marginModel: s.margin_mae.model.toFixed(2),
    marginClose: s.margin_mae.close.toFixed(2),
    edgeR: formatFixed(s.spread_edge_corr.r, 2),
    edgeCi: formatInterval([lo, hi], 2),
    edgeExact:
      "Correlation of (model spread − closing line) with (result − closing line): " +
      `r ${formatFixed(s.spread_edge_corr.r, 3)} ${formatInterval([lo, hi], 3)}, ` +
      `n ${formatCount(s.spread_edge_corr.n)}`,
    validation: anyValidated
      ? `validated: ${[
          ...(s.spread_edge_corr.validated ? ["pooled spread"] : []),
          ...s.validated_buckets,
        ].join(", ")}`
      : "not validated",
  };
}
