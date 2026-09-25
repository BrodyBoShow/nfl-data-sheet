import type { Card } from "@/lib/card";
import { formatFixed, formatSpread } from "@/lib/format";
import { formatSignalValue, signalLabel } from "@/lib/signal-labels";

// Model arithmetic (docs/phases/P6.md §5, §6). For each side: α + home field + β ×
// centered value per input = projected points. Every number is the card's own
// (projection.decomposition). Nothing is re-derived here.

type Projection = NonNullable<Card["projection"]>;
type Side = Projection["decomposition"]["home"];

const DASH = <span className="null">—</span>;

function SideTable({ team, side, isHome }: { team: string; side: Side; isHome: boolean }) {
  return (
    <div className="table-scroll">
      <table className="data arithmetic-table" data-side={isHome ? "home" : "away"}>
        <caption className="t-small ink-2">{team} projected points</caption>
        <thead>
          <tr>
            <th scope="col">Input</th>
            <th scope="col" className="num">Value</th>
            <th scope="col" className="num">Week mean</th>
            <th scope="col" className="num">Centered</th>
            <th scope="col" className="num keep-case">β</th>
            <th scope="col" className="num">Contribution</th>
            <th scope="col" className="num">n</th>
            <th scope="col" className="num">Stab</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">Intercept (α)</th>
            <td className="num">{DASH}</td>
            <td className="num">{DASH}</td>
            <td className="num">{DASH}</td>
            <td className="num">{DASH}</td>
            <td className="num" data-contribution>{formatFixed(side.alpha, 2)}</td>
            <td className="num">{DASH}</td>
            <td className="num">{DASH}</td>
          </tr>
          <tr>
            <th scope="row">Home field</th>
            <td className="num">{DASH}</td>
            <td className="num">{DASH}</td>
            <td className="num">{DASH}</td>
            <td className="num">{DASH}</td>
            <td className="num" data-contribution>{formatFixed(side.hfa, 2)}</td>
            <td className="num">{DASH}</td>
            <td className="num">{DASH}</td>
          </tr>
          {side.terms.map((t) => (
            <tr key={`${t.team}-${t.signal}`}>
              <th scope="row">
                {t.team} {t.role === "offense" ? "offense" : "defense"} · {signalLabel(t.signal)}
                <span className="raw-name mono t-small ink-3"> {t.signal}</span>
              </th>
              <td className="num">{formatSignalValue(t.signal, t.value)}</td>
              <td className="num">{formatSignalValue(t.signal, t.week_mean)}</td>
              <td className="num">{formatFixed(t.centered, 3)}</td>
              <td className="num">{formatFixed(t.beta, 2)}</td>
              <td className="num" data-contribution>{formatFixed(t.contribution, 2)}</td>
              <td className="num">{t.sample_n ?? DASH}</td>
              <td className="num">{formatFixed(t.stability, 2)}</td>
            </tr>
          ))}
          <tr className="total-row">
            <th scope="row">Projected points</th>
            <td className="num" colSpan={4} />
            <td className="num" data-points>{formatFixed(side.points, 1)}</td>
            <td className="num" colSpan={2} />
          </tr>
        </tbody>
      </table>
    </div>
  );
}

export function Arithmetic({ card, projection }: { card: Card; projection: Projection }) {
  const { home_team: home, away_team: away } = card.identity;
  const d = projection.decomposition;
  return (
    <section className="section" aria-labelledby="arith-h">
      <h2 id="arith-h" className="section-label t-cap">
        How the model got there
      </h2>
      <SideTable team={home} side={d.home} isHome />
      <SideTable team={away} side={d.away} isHome={false} />
      <p className="legend t-small ink-2 mono">
        {home} {formatFixed(projection.pts_home, 1)} · {away} {formatFixed(projection.pts_away, 1)} → spread{" "}
        {formatSpread(projection.spread_home, home, away, "model")} · total {formatFixed(projection.total, 1)} ·
        model {projection.model_version}
      </p>
      <p className="legend t-small ink-2">
        Inputs are season-to-date and opponent-adjusted, entering this week. Centered = value
        minus that week&apos;s league mean.
      </p>
    </section>
  );
}
