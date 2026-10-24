// /method view model (docs/phases/P6.md §3, §5, step 7). Every figure comes from the
// build-time backtest summary. The report itself renders only inside ReportFrame
// (components/method/report-frame.tsx), under a provenance heading and this preface.
import { backtest, type BacktestSummary, type EdgeInterval, type WinRateBucket } from "./backtest";
import { formatCount, formatFixed, formatInterval, formatSeasonRange } from "./format";

/** Break-even share of decided games at the standard −110 price: risk 110 to win 100,
 *  so a side has to win 110 of every 210. */
export const BREAKEVEN_110 = 110 / 210;

const pct = (x: number) => `${(x * 100).toFixed(1)}%`;
const bucketLabel = (b: WinRateBucket) => `${b.market}, ${b.bucket} pt |edge|, ${formatCount(b.decided)} decided`;

/** Exact P(Binomial(n, ½) ≥ k): the chance a side that wins half its games still reaches
 *  k wins in n. n is a few hundred at most, so a direct sum is fine. */
export function coinFlipTail(n: number, k: number): number {
  let logPmf = -n * Math.LN2; // log P(0 wins)
  let tail = 0;
  for (let j = 0; j <= n; j++) {
    if (j >= k) tail += Math.exp(logPmf);
    if (j < n) logPmf += Math.log((n - j) / (j + 1));
  }
  return tail;
}

/** How a bucket at or above break-even compares with a coin flip: points above 50%, the
 *  binomial standard error at 50% for its decided games, and the distance in SEs. */
export function againstCoinFlip(b: WinRateBucket): { points: number; se: number; z: number } {
  const share = b.wins / b.decided;
  const se = Math.sqrt(0.25 / b.decided);
  return { points: (share - 0.5) * 100, se: se * 100, z: (share - 0.5) / se };
}

/** Across every bucket, if the model's side won exactly half the time: the expected number
 *  at or above break-even, and the chance at least one is. Buckets treated as independent;
 *  spread and total buckets share games, so it's approximate. */
export function chanceAtOrAbove(buckets: WinRateBucket[]): { expected: number; atLeastOne: number } {
  const ps = buckets.map((b) => coinFlipTail(b.decided, Math.ceil(b.decided * BREAKEVEN_110 - 1e-9)));
  return {
    expected: ps.reduce((a, p) => a + p, 0),
    atLeastOne: 1 - ps.reduce((a, p) => a * (1 - p), 1),
  };
}

function atOrAboveVerdict(atOrAbove: WinRateBucket[], all: WinRateBucket[], breakeven: string): string {
  const n = atOrAbove.length;
  const listed = atOrAbove
    .map((b) => `${pct(b.rate)} (${b.market}, ${b.bucket} pt |edge|, ${formatCount(b.wins)} of ${formatCount(b.decided)} decided)`)
    .join("; ");
  const margins = atOrAbove.map((b) => {
    const { points, se, z } = againstCoinFlip(b);
    const verdict =
      z < 1
        ? "far too few games to tell it apart from chance"
        : z < 1.96
          ? "too few games to tell it apart from chance"
          : "unlikely at a true 50% on its own, though the count below still applies";
    const subject = n === 1 ? "That's" : `${pct(b.rate)} (${b.market}, ${b.bucket} pt) is`;
    return (
      `${subject} ${points.toFixed(1)} points above a coin flip. With ${formatCount(b.decided)} decided ` +
      `games, the standard error of that share is ${se.toFixed(1)} points, so it sits ${z.toFixed(1)} ` +
      `standard errors above 50%: ${verdict}.`
    );
  });
  const { expected, atLeastOne } = chanceAtOrAbove(all);
  return [
    `${n} of ${all.length} buckets ${n === 1 ? "reaches" : "reach"} it: ${listed}.`,
    ...margins,
    `${n === 1 ? "It's" : "They're"} also ${n} of ${all.length} buckets examined. If the model's side won ` +
      `exactly half the time, about ${Math.round(expected)} of ${all.length} would reach ${breakeven} by ` +
      `chance, and at least one would do so about ${Math.round(atLeastOne * 100)}% of the time.`,
    "Every bucket is shown, and the correlations above are the test.",
  ].join(" ");
}

/** Exact P(Binomial(n, p) ≥ k). */
export function binomialTail(n: number, k: number, p: number): number {
  let pmf = (1 - p) ** n; // P(0)
  let tail = 0;
  for (let j = 0; j <= n; j++) {
    if (j >= k) tail += pmf;
    if (j < n) pmf *= ((n - j) / (j + 1)) * (p / (1 - p));
  }
  return tail;
}

const excludesZero = (e: EdgeInterval) => e.ci_low > 0 || e.ci_high < 0;
const describeInterval = (e: EdgeInterval) =>
  `${e.market} ${e.slice === "All" ? "all games" : e.slice}, r ${formatFixed(e.r, 3)} ` +
  formatInterval([e.ci_low, e.ci_high], 3);

/** What the backtest's edge intervals say together (docs/phases/P7.md, open item 22). Every
 *  clause follows the data: how many exclude zero, how many chance alone would give at
 *  95%, which side of zero they're on, and whether that's evidence of anything. */
export function edgeIntervalsSentences(s: BacktestSummary = backtest): string[] {
  const all = s.edge_intervals;
  if (all.length === 0) throw new Error("backtest summary has no edge intervals");
  const n = all.length;
  const out = all.filter(excludesZero);
  const k = out.length;
  const expected = n * 0.05;
  const tail = binomialTail(n, k, 0.05);
  const aboutChance = k === 0 || tail >= 0.05;
  const below = out.filter((e) => e.ci_high < 0).length;
  const sentences = [
    `The backtest report tests ${n} of these intervals: all games, each stability bucket and each ` +
      "test season, for the spread and the total.",
    k === 0
      ? "None excludes zero."
      : `${k} ${k === 1 ? "excludes" : "exclude"} zero: ${out.map(describeInterval).join("; ")}.`,
    `At 95%, about ${formatFixed(expected, 0)} of ${n} would exclude zero by chance alone` +
      (aboutChance
        ? ", so this is what chance produces."
        : `, and ${k} or more would happen by chance only ${Math.round(tail * 100)}% of the time.`),
  ];
  if (k > 0) {
    sentences.push(
      below === k
        ? `${k === 1 ? "It's" : "Each is"} below zero: there, the model's side did worse against the closing line.`
        : `${below} ${below === 1 ? "is" : "are"} below zero, where the model's side did worse, and ` +
            `${k - below} above.`,
    );
  }
  sentences.push(
    aboutChance
      ? "It is not evidence of an edge in either direction."
      : "It still isn't validation, which needs an interval entirely above zero.",
  );
  return sentences;
}

/** The correction's headline counts, for /method and the status-line tooltip. */
export function correctionCounts(rows: CorrectionRow[]): { total: number; changed: number; unchanged: number } {
  const changed = rows.filter((r) => !r.unchanged).length;
  return { total: rows.length, changed, unchanged: rows.length - changed };
}

/** The status-line marker's tooltip. It travels with the numbers (P7 open item 19). */
export function correctionTooltip(earlier: BacktestSummary, now: BacktestSummary): string {
  const { total, changed } = correctionCounts(correctionRows(earlier, now));
  return (
    `Corrected: until ${now.report.generated_utc.slice(0, 10)}, computed with an offensive-line input that ` +
    `was wrong for some teams in 2020–2021. ${changed} of ${total} figures on /method changed at the ` +
    "precision shown, and the rest are unchanged."
  );
}

export interface CorrectionRow {
  figure: string;
  earlier: string;
  now: string;
  unchanged: boolean; // equal at the precision shown
}

/** The figures /method shows, earlier (computed on the wrong input) beside now, each at
 *  the precision the page prints it. `unchanged` compares those printed strings, so a
 *  change too small to show reads as unchanged, and says so. */
export function correctionRows(earlier: BacktestSummary, now: BacktestSummary): CorrectionRow[] {
  const rows: [string, (s: BacktestSummary) => string][] = [
    ["Margin MAE, model", (s) => s.margin_mae.model.toFixed(2)],
    ["Margin MAE, closing line", (s) => s.margin_mae.close.toFixed(2)],
    [
      "Spread r, all games [95% CI]",
      (s) => `${formatFixed(s.spread_edge_corr.r, 3)} ${formatInterval(s.spread_edge_corr.ci as [number, number], 3)}`,
    ],
    ["Typical miss, about … points", (s) => formatFixed(Math.min(...Object.values(s.buckets).map((b) => b.margin_sd)), 0)],
    ["α (SE)", (s) => `${formatFixed(s.model.alpha.value, 2)} (${formatFixed(s.model.alpha.se, 2)})`],
    ["β off (SE)", (s) => `${formatFixed(s.model.beta_off.value, 2)} (${formatFixed(s.model.beta_off.se, 2)})`],
    ["β def (SE)", (s) => `${formatFixed(s.model.beta_def.value, 2)} (${formatFixed(s.model.beta_def.se, 2)})`],
    ["γ (SE)", (s) => `${formatFixed(s.model.gamma.value, 2)} (${formatFixed(s.model.gamma.se, 2)})`],
    ["Games fit", (s) => formatCount(s.model.n_games)],
    ["Stability: low below", (s) => formatFixed(s.buckets.mid!.stability_range[0]!, 2)],
    ["Stability: mid below", (s) => formatFixed(s.buckets.high!.stability_range[0]!, 2)],
    ["Lowest input stability", (s) => formatFixed(s.stability_floor, 2)],
    ...(["low", "mid", "high"] as const).flatMap((b): [string, (s: BacktestSummary) => string][] => {
      const B = b.toUpperCase();
      return [
        [`${B}: games`, (s) => formatCount(s.buckets[b]!.n_games)],
        [`${B}: typical miss, margin (pts)`, (s) => formatFixed(s.buckets[b]!.margin_sd, 1)],
        [
          `${B}: spread r [95% CI]`,
          (s) => `${formatFixed(s.buckets[b]!.spread.corr, 3)} ${formatInterval([s.buckets[b]!.spread.ci_low, s.buckets[b]!.spread.ci_high], 3)}`,
        ],
        [
          `${B}: total r [95% CI]`,
          (s) => `${formatFixed(s.buckets[b]!.total.corr, 3)} ${formatInterval([s.buckets[b]!.total.ci_low, s.buckets[b]!.total.ci_high], 3)}`,
        ],
      ];
    }),
    [
      "Edge intervals excluding zero",
      (s) => `${s.edge_intervals.filter(excludesZero).length} of ${s.edge_intervals.length}`,
    ],
  ];
  return rows.map(([figure, f]) => {
    const [e, n] = [f(earlier), f(now)];
    return { figure, earlier: e, now: n, unchanged: e === n };
  });
}

export interface ReportFrameView {
  heading: string;
  breakeven: string; // "52.4%"
  highest: WinRateBucket;
  atOrAbove: WinRateBucket[]; // buckets at or above break-even (p5-v2: spread 2–3 pt only)
  preface: string[]; // app-authored sentences, in order
}

export function reportFrameView(s: BacktestSummary = backtest): ReportFrameView {
  if (s.win_rates.length === 0) throw new Error("backtest summary has no win-rate buckets");
  const highest = s.win_rates.reduce((a, b) => (b.rate > a.rate ? b : a));
  const atOrAbove = s.win_rates.filter((b) => b.rate >= BREAKEVEN_110);
  const breakeven = pct(BREAKEVEN_110);
  const verdict =
    atOrAbove.length === 0
      ? `No bucket reaches it. The highest is ${pct(highest.rate)} (${bucketLabel(highest)}).`
      : atOrAboveVerdict(atOrAbove, s.win_rates, breakeven);
  return {
    heading:
      `Backtest report — generated by ${s.report.script}, ${s.report.generated_utc} UTC, ` +
      `from ${formatCount(s.n_games)} out-of-sample games`,
    breakeven,
    highest,
    atOrAbove,
    preface: [
      `Below is the backtest report exactly as ${s.report.script} wrote it.`,
      `Its two tables by |edge| are backtest win rates: how often the model's side of the ` +
        `closing line won, ${formatSeasonRange(s.test_seasons)}.`,
      `At the standard −110 price, a side has to win ${breakeven} of decided games to break ` +
        `even (110 ÷ 210). ${verdict}`,
      "These rates are evidence that the edge doesn't work. They are not a record of anything this site has done.",
    ],
  };
}
