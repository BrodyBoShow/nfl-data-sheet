// Build-time backtest content (docs/phases/P6.md step 4). Runs the parser on the real
// committed report and coefficients. If scripts/backtest.py is re-run and the numbers
// change, the exact-value assertions below are meant to fail and be updated
// deliberately.
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { buildSummary } from "../scripts/backtest-content.mjs";
import { statusLine, type BacktestSummary } from "../lib/backtest";
import { formatFixed, formatSeasonRange } from "../lib/format";

const report = readFileSync(new URL("../../docs/backtest_report.md", import.meta.url), "utf8");
const coefficients = JSON.parse(
  readFileSync(new URL("../../pipeline/synthesis/model_coefficients.json", import.meta.url), "utf8"),
);
const summarize = (md = report, coef = coefficients): BacktestSummary => ({
  ...buildSummary(md, coef),
  sources: {},
});

describe("buildSummary on the committed report", () => {
  it("reads the out-of-sample margin MAE and the pooled spread edge correlation", () => {
    const s = summarize();
    expect(s.n_games).toBe(1615);
    expect(s.fit_seasons).toEqual([2019, 2020, 2021, 2022, 2023, 2024, 2025]);
    expect(s.test_seasons).toEqual([2020, 2021, 2022, 2023, 2024, 2025]);
    expect(s.margin_mae).toEqual({ model: 10.32, close: 9.76 });
    expect(s.spread_edge_corr).toEqual({ r: -0.032, ci: [-0.08, 0.016], n: 1615, validated: false });
    expect(s.validated_buckets).toEqual([]);
    expect(Object.keys(s.buckets).sort()).toEqual(["high", "low", "mid"]);
    expect(s.stability_floor).toBe(0.2262); // low bucket's stability_range[0]
  });

  it("produces the status line figures (step 4 done-when)", () => {
    const line = statusLine(summarize());
    expect(line.scope).toBe("Backtest 2020–25, 1,615 games out of sample");
    expect([line.marginModel, line.marginClose, line.edgeR, line.edgeCi]).toEqual([
      "10.32", "9.76", "−0.03", "[−0.08, 0.02]",
    ]);
    expect(line.edgeExact).toBe(
      "Correlation of (model spread − closing line) with (result − closing line): " +
        "r −0.032 [−0.080, 0.016], n 1,615",
    );
    expect(line.validation).toBe("not validated");
  });
});

describe("buildSummary fails loudly instead of misreading", () => {
  const broken: [string, () => unknown, RegExp][] = [
    ["a renamed heading", () => summarize(report.replace("### Margin (home − away)", "### Margin")), /exactly one "### Margin/],
    ["a duplicated heading", () => summarize(report + "\n### Spread: correlation\n"), /found 2/],
    ["a renamed column", () => summarize(report.replace("| Close MAE | Close RMSE |", "| Closing MAE | Close RMSE |")), /no "Close MAE" column/],
    ["a missing All row", () => summarize(report.replace(/^\| \*\*All\*\* \| 1615 \| 10\.32 .*$/m, "")), /expected one "\*\*All\*\*" row/],
    ["a non-numeric cell", () => summarize(report.replace("| **All** | 1615 | 10.32 |", "| **All** | 1615 | n/a |")), /not a number/],
    ["a malformed CI", () => summarize(report.replace("| All | -0.032 | [-0.080, 0.016] |", "| All | -0.032 | -0.080 to 0.016 |")), /not an interval/],
    ["an unknown Validated value", () => summarize(report.replace("| [-0.080, 0.016] | 1615 | no |", "| [-0.080, 0.016] | 1615 | maybe |")), /Validated/],
    ["report and coefficients disagreeing on n", () => summarize(report, { ...coefficients, calibration: { ...coefficients.calibration, n_games: 1600 } }), /disagree: margin n 1615 vs calibration.n_games 1600/],
    ["report and coefficients disagreeing on seasons", () => summarize(report, { ...coefficients, calibration: { ...coefficients.calibration, test_seasons: [2021, 2022] } }), /disagree: report seasons/],
    ["coefficients without calibration", () => summarize(report, { model_version: "x" }), /no calibration block/],
    ["coefficients without fit_seasons", () => summarize(report, { ...coefficients, fit_seasons: undefined }), /no fit_seasons/],
    [
      "a bucket without a stability_range",
      () => summarize(report, { ...coefficients, calibration: { ...coefficients.calibration, buckets: { ...coefficients.calibration.buckets, low: { ...coefficients.calibration.buckets.low, stability_range: undefined } } } }),
      /no stability_range/,
    ],
  ];
  it.each(broken)("throws on %s", (_, run, message) => {
    expect(run).toThrow(message);
  });
});

describe("the status line's validation wording follows the data", () => {
  it("names validated buckets rather than saying 'not validated'", () => {
    const s = { ...summarize(), validated_buckets: ["low spread"] };
    expect(statusLine(s).validation).toBe("validated: low spread");
  });
});

describe("format helpers", () => {
  it("uses a true minus and never shows a negative zero", () => {
    expect(formatFixed(-0.032, 2)).toBe("−0.03");
    expect(formatFixed(-0.004, 2)).toBe("0.00");
    expect(formatFixed(0.016, 2)).toBe("0.02");
  });
  it("collapses only contiguous season ranges", () => {
    expect(formatSeasonRange([2020, 2021, 2022])).toBe("2020–22");
    expect(formatSeasonRange([2020, 2022])).toBe("2020, 2022");
    expect(formatSeasonRange([2025])).toBe("2025");
  });
});
