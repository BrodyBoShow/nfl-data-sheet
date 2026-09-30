// The /method preface's arithmetic (lib/method.ts). Expected values were computed
// independently in Python (exact binomial via math.comb) on 2026-09-30 against the p5-v2
// report: spread 2–3 pt is 151 of 287.
import { describe, expect, it } from "vitest";

import { backtest, type WinRateBucket } from "../lib/backtest";
import { BREAKEVEN_110, againstCoinFlip, chanceAtOrAbove, coinFlipTail } from "../lib/method";

const spread23: WinRateBucket = { market: "spread", bucket: "2–3", decided: 287, wins: 151, rate: 0.526 };

describe("coinFlipTail", () => {
  it("is an exact binomial tail at p = ½", () => {
    expect(coinFlipTail(10, 0)).toBeCloseTo(1, 12);
    expect(coinFlipTail(10, 11)).toBe(0);
    expect(coinFlipTail(4, 2)).toBeCloseTo(11 / 16, 12);
  });

  it("gives the chance a 50% side reaches break-even in 287 decided games", () => {
    const k = Math.ceil(287 * BREAKEVEN_110 - 1e-9);
    expect(k).toBe(151);
    expect(coinFlipTail(287, k)).toBeCloseTo(0.204, 3);
  });
});

describe("againstCoinFlip", () => {
  it("measures 151 of 287 against 50% in points and standard errors", () => {
    const { points, se, z } = againstCoinFlip(spread23);
    expect(points).toBeCloseTo(2.613, 3);
    expect(se).toBeCloseTo(2.951, 3); // binomial SE at 50%, in points
    expect(z).toBeCloseTo(0.885, 3);
  });
});

describe("chanceAtOrAbove", () => {
  it("counts how many of the committed report's ten buckets a 50% side would put at break-even", () => {
    expect(backtest.win_rates).toHaveLength(10);
    const { expected, atLeastOne } = chanceAtOrAbove(backtest.win_rates);
    expect(expected).toBeCloseTo(1.99, 2);
    expect(atLeastOne).toBeCloseTo(0.892, 3);
  });
});
