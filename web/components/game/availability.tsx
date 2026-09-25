import type { Card } from "@/lib/card";

// Availability counts (docs/phases/P6.md §6, context). The Availability analyst writes a
// cluster count only for teams with at least one flagged player (docs/signals.md), so a
// missing row means none flagged OR not computed for this week. It shows "—" and is
// never shown as 0.

const DASH = <span className="null">—</span>;
const MISSING = "No row: none flagged, or not computed this week";

export function Availability({ card }: { card: Card }) {
  const a = card.context.availability;
  const teams = [card.identity.away_team, card.identity.home_team];
  const cell = (team: string, k: string) => {
    const v = a[team]?.[k] ?? null;
    return v === null ? <span title={MISSING}>{DASH}</span> : String(v);
  };
  return (
    <section className="section" aria-labelledby="avail-h">
      <h2 id="avail-h" className="section-label t-cap">
        Availability · not used by the model
      </h2>
      <table className="data avail-table">
        <thead>
          <tr>
            <th scope="col">Team</th>
            <th scope="col" className="num">O-line flagged</th>
            <th scope="col" className="num">Secondary flagged</th>
          </tr>
        </thead>
        <tbody>
          {teams.map((t) => (
            <tr key={t}>
              <th scope="row" className="mono">{t}</th>
              <td className="num">{cell(t, "ol_cluster_count")}</td>
              <td className="num">{cell(t, "secondary_cluster_count")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="legend t-small ink-2">
        Players listed with an injury designation or as unavailable (suspension, exempt list).
      </p>
    </section>
  );
}
