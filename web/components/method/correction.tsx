import type { BacktestSummary } from "@/lib/backtest";
import { correctionCounts, correctionRows } from "@/lib/method";

// /method's correction (docs/phases/P7.md, open item 22, row 3). The 2026-10-18 notice
// promised the earlier figures beside the new ones once corrected; this is that. Every
// figure is printed by the same formatter as the rest of the page, earlier from the
// archived p5-v2 artifacts (docs/backtest_archive/p5-v2/), and "unchanged" compares the
// printed strings. Takes both summaries as props so honesty.test.ts 16 can render it with
// a figure that moved.

export function Correction({ earlier, now }: { earlier: BacktestSummary; now: BacktestSummary }) {
  const rows = correctionRows(earlier, now);
  const counts = correctionCounts(rows);
  return (
    <section
      className="section prose t-prose input-notice"
      id="correction"
      aria-labelledby="correction-h"
      data-correction="true"
    >
      <h2 id="correction-h" className="section-label t-cap warn">
        Corrected: an input to these backtest numbers was wrong
      </h2>
      <p>
        Until {now.report.generated_utc.slice(0, 10)}, every backtest figure on this page and in the line at the
        top of every page was computed with an offensive-line continuity input that was wrong for some teams in
        2020 and 2021. The 2020 snap counts label some linemen with combined codes such as G/T and C/G, and the
        ratings didn&apos;t recognize them, so those linemen were left out. The input was wrong in 75 of 544
        team-weeks in 2020 and 132 of 576 in 2021, counting the regular-season weeks the model&apos;s fit reads.
        Until 2026-10-28 this said 119 of 672 and 172 of 704: a count from a script that wasn&apos;t kept, which
        can&apos;t be reproduced. The new count comes from the rating runs&apos; own logs.
      </p>
      <p>
        The input measures how much of a team&apos;s offensive line returned from the year before. It sets how
        much weight last season gets in every offensive rating, including the EPA per play the projection uses.
        So it reached every out-of-sample season, and the coefficients and stability buckets today&apos;s
        projections use.
      </p>
      <p>
        The ratings were rebuilt with those linemen counted, the model refit ({earlier.model_version} to{" "}
        {now.model_version}), and the backtest rerun.{" "}
        {counts.changed === 0
          ? `None of the ${counts.total} figures below changed at the precision this page shows.`
          : `Of the ${counts.total} figures below, ${counts.unchanged} are unchanged at the precision this page ` +
            `shows and ${counts.changed} changed.`}{" "}
        The earlier column was computed on the wrong input.
      </p>
      <div className="table-scroll">
        <table className="data" data-correction-table="true">
          <thead>
            <tr>
              <th scope="col" className="sticky">Figure</th>
              <th scope="col" className="num">Earlier, wrong input ({earlier.model_version})</th>
              <th scope="col" className="num">Now ({now.model_version})</th>
              <th scope="col"></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.figure} data-unchanged={r.unchanged ? "true" : "false"}>
                <th scope="row" className="sticky">{r.figure}</th>
                <td className="num">{r.earlier}</td>
                <td className="num">{r.now}</td>
                <td className="mono">{r.unchanged ? "unchanged" : "changed"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
