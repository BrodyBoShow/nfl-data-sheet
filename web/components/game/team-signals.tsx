import type { SignalRowT } from "@/lib/db";
import { anyLeaguePct, isLowStability } from "@/lib/game";
import { signalLabel } from "@/lib/signal-labels";

import { LowStabilityNote, UnitCells, UnitHeads } from "./signal-cells";

// Games without a card (P6.md §3): the efficiency signals entering the week, one row per
// signal with both teams side by side. This is a display grouping of Q6 rows by team,
// not the off/def opponent pairing (that comes only from the card, built by the
// synthesizer's pair_unit_signals).

export function TeamSignals({ rows, home, away }: { rows: SignalRowT[]; home: string; away: string }) {
  const eff = rows.filter((r) => r.sector === "efficiency" && r.game_id === null);
  const signals = [...new Set(eff.map((r) => r.signal))].sort((a, b) => {
    const unit = (s: string) => (s.endsWith("_off") ? 0 : 1);
    return unit(a) - unit(b) || a.localeCompare(b);
  });
  const get = (team: string, s: string) => eff.find((r) => r.team === team && r.signal === s);
  const showPct = anyLeaguePct(eff.map((r) => r.league_pct));
  return (
    <section className="section" aria-labelledby="teamsig-h">
      <h2 id="teamsig-h" className="section-label t-cap">
        Efficiency entering the week
      </h2>
      <div className="table-scroll">
        <table className="data team-signals-table">
          <thead>
            <tr>
              <th scope="col" className="sticky">Signal</th>
              <UnitHeads label={away} showPct={showPct} />
              <UnitHeads label={home} showPct={showPct} />
            </tr>
          </thead>
          <tbody>
            {signals.map((s) => (
              <tr key={s}>
                <th scope="row" className="sticky">
                  {signalLabel(s)} ({s.endsWith("_off") ? "offense" : "defense"})
                  <span className="raw-name mono t-small ink-3"> {s}</span>
                </th>
                <UnitCells u={get(away, s)} showPct={showPct} />
                <UnitCells u={get(home, s)} showPct={showPct} />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <LowStabilityNote show={eff.some((r) => r.value !== null && isLowStability(r.stability))} />
    </section>
  );
}
