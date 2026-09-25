import type { Metadata } from "next";
import Link from "next/link";

import { ReportFrame } from "@/components/method/report-frame";
import { backtest, statusLine } from "@/lib/backtest";
import { formatCount, formatFixed, formatInterval, formatSeasonRange } from "@/lib/format";

// /method (docs/phases/P6.md §3, step 7): how the projection is built, what input
// stability and the edge tag mean, then the backtest report in its frame. Build-time
// content only; no database reads.

export const metadata: Metadata = { title: "Method and backtest" };

const BUCKETS = ["low", "mid", "high"] as const;

export default function MethodPage() {
  const s = backtest;
  const m = s.model;
  const line = statusLine(s);
  const buckets = BUCKETS.map((name) => ({ name, ...s.buckets[name]! }));
  const [lo, hi] = s.spread_edge_corr.ci as [number, number];
  const anyValidated = s.spread_edge_corr.validated || s.validated_buckets.length > 0;
  const coef = (c: { value: number; se: number }) => (
    <>
      <td className="num">{formatFixed(c.value, 2)}</td>
      <td className="num">{formatFixed(c.se, 2)}</td>
    </>
  );
  return (
    <>
      <div className="page-head">
        <h1 className="t-title">Method</h1>
        <p className="t-small ink-2">
          How the projection is built, what the numbers beside it mean, and the backtest behind the status
          line.
        </p>
      </div>

      <section className="section prose t-prose" id="model" aria-labelledby="model-h">
        <h2 id="model-h" className="section-label t-cap">
          The projection
        </h2>
        <p>
          Each team&apos;s points come from two ratings entering the week, both opponent-adjusted: its
          offense&apos;s EPA per play, and its opponent&apos;s defense&apos;s EPA per play allowed. Each is
          centered on that week&apos;s league mean.
        </p>
        <p className="mono t-base formula">
          points = α + β<sub>off</sub> × (offense − mean) + β<sub>def</sub> × (opponent defense − mean) + γ × h
        </p>
        <div className="table-scroll">
          <table className="data">
            <thead>
              <tr>
                <th scope="col" className="sticky">Term</th>
                <th scope="col" className="num">Estimate</th>
                <th scope="col" className="num">SE</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <th scope="row" className="sticky">α, points per team at league-mean ratings</th>
                {coef(m.alpha)}
              </tr>
              <tr>
                <th scope="row" className="sticky">β off, per EPA/play of offense</th>
                {coef(m.beta_off)}
              </tr>
              <tr>
                <th scope="row" className="sticky">β def, per EPA/play allowed by the opponent</th>
                {coef(m.beta_def)}
              </tr>
              <tr>
                <th scope="row" className="sticky">γ, home field (h = +½ home, −½ away, 0 neutral)</th>
                {coef(m.gamma)}
              </tr>
            </tbody>
          </table>
        </div>
        <p>
          Fit on {formatCount(m.n_games)} regular-season games, {formatSeasonRange(s.fit_seasons)}, on{" "}
          {m.fitted_on}. The spread is the away team&apos;s points minus the home team&apos;s, and the total is
          their sum. Every game view shows this arithmetic with the card&apos;s own numbers.
        </p>
        <p>
          Nothing else on a card enters the projection. The market, weather, rest, travel, availability and
          the other efficiency splits are shown as context only.
        </p>
      </section>

      <section className="section prose t-prose" id="stability" aria-labelledby="stability-h">
        <h2 id="stability-h" className="section-label t-cap">
          Input stability
        </h2>
        <p>
          Every efficiency rating blends three things: this season&apos;s plays, last season&apos;s rating,
          and the league average. Its stability is the share that isn&apos;t the league average. At 1.00 none
          of it is; at 0.20, 80% of it is.
        </p>
        <p>
          A game&apos;s input stability is the lowest of its four model inputs. The buckets are thirds of the
          backtest games: low below {formatFixed(buckets[1]!.stability_range[0]!, 2)}, mid below{" "}
          {formatFixed(buckets[2]!.stability_range[0]!, 2)}, high above that. It describes the inputs, not
          confidence in the result: the typical miss is about the same in every bucket.
        </p>
        <div className="table-scroll">
          <table className="data">
            <thead>
              <tr>
                <th scope="col" className="sticky">Bucket</th>
                <th scope="col" className="num">Stability</th>
                <th scope="col" className="num">Games</th>
                <th scope="col" className="num">Typical miss, margin (pts)</th>
              </tr>
            </thead>
            <tbody>
              {buckets.map((b) => (
                <tr key={b.name}>
                  <th scope="row" className="sticky mono">{b.name.toUpperCase()}</th>
                  <td className="num">
                    {formatFixed(b.stability_range[0]!, 2)}–{formatFixed(b.stability_range[1]!, 2)}
                  </td>
                  <td className="num">{formatCount(b.n_games)}</td>
                  <td className="num">{formatFixed(b.margin_sd, 1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p>
          In the context tables, a rating below {formatFixed(s.stability_floor, 2)}, the lowest input stability
          of any backtested game, is shown in grey. It is mostly league average.
        </p>
      </section>

      <section className="section prose t-prose" id="edge" aria-labelledby="edge-h">
        <h2 id="edge-h" className="section-label t-cap">
          Model − market, and why it&apos;s marked not validated
        </h2>
        <p>
          The game view shows how far the model&apos;s line is from the market&apos;s, and in which direction.
          It would count as validated if, across the backtest games, that difference moved with the result
          measured against the closing line, with a 95% interval entirely above zero.
        </p>
        <p>
          {anyValidated ? "Pooled, it doesn't." : "It doesn't."} Pooled over{" "}
          {formatCount(s.spread_edge_corr.n)} games, the spread correlation is r{" "}
          {formatFixed(s.spread_edge_corr.r, 3)} {formatInterval([lo, hi], 3)}. The model&apos;s margin error
          (MAE {line.marginModel}) is also larger than the closing line&apos;s ({line.marginClose}) over the same
          games. By stability bucket:
        </p>
        <div className="table-scroll">
          <table className="data">
            <thead>
              <tr>
                <th scope="col" className="sticky">Bucket</th>
                <th scope="col" className="num">Games</th>
                <th scope="col" className="num">Spread r [95% CI]</th>
                <th scope="col" className="num">Total r [95% CI]</th>
                <th scope="col">Validated</th>
              </tr>
            </thead>
            <tbody>
              {buckets.map((b) => (
                <tr key={b.name}>
                  <th scope="row" className="sticky mono">{b.name.toUpperCase()}</th>
                  <td className="num">{formatCount(b.spread.n)}</td>
                  <td className="num">
                    {formatFixed(b.spread.corr, 3)} {formatInterval([b.spread.ci_low, b.spread.ci_high], 3)}
                  </td>
                  <td className="num">
                    {formatFixed(b.total.corr, 3)} {formatInterval([b.total.ci_low, b.total.ci_high], 3)}
                  </td>
                  <td className="mono">
                    spread {b.spread.validated ? "yes" : "no"} · total {b.total.validated ? "yes" : "no"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p>
          Every Model − market figure on this site carries its tag in the same cell:{" "}
          {anyValidated ? (
            <>
              <span className="mono">VALIDATED</span> only where the row above says yes,{" "}
              <span className="mono">NOT VALIDATED</span> everywhere else.
            </>
          ) : (
            <>
              <span className="mono">NOT VALIDATED</span>, in every bucket.
            </>
          )}{" "}
          The tag&apos;s tooltip gives the figures for that game&apos;s bucket. Results land about{" "}
          {formatFixed(Math.min(...buckets.map((b) => b.margin_sd)), 0)} points from any projection, the
          model&apos;s or the market&apos;s, which is why the game view prints the typical miss beside every
          line.
        </p>
        <p>
          <Link href="#report">The full backtest report</Link> is below.
        </p>
      </section>

      <ReportFrame />
    </>
  );
}
