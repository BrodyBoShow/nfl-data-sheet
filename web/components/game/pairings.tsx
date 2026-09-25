import type { CardBrief, CardPairing } from "@/lib/card";
import { anyLeaguePct } from "@/lib/game";
import { signalLabel } from "@/lib/signal-labels";

import { UnitCells, UnitHeads } from "./signal-cells";

// Efficiency pairings (docs/phases/P6.md §6, context, not used by the model). The rows
// are the card's own pair_unit_signals output: a subject unit's `_off` signal beside
// the opponent's matching `_def`.
//
// P7 slot: `subjectLabel` is the one place a row gets its name. It returns the team code
// now and would return a player name once player rows (player_id set) exist.
// The league-percentile column appears only once some brief in the table carries
// league_pct (see signal-cells.tsx).

export function subjectLabel(b: Pick<CardBrief, "team" | "player_id">): string {
  return b.team ?? b.player_id ?? "—";
}

export function PairingTable({ rows, subject, opponent }: { rows: CardPairing[]; subject: string; opponent: string }) {
  if (rows.length === 0) {
    return (
      <p className="t-small ink-2">
        {subject} offense vs. {opponent} defense: no efficiency signals on the card for this week yet.
      </p>
    );
  }
  const showPct = anyLeaguePct(rows.flatMap((r) => [r.subject.league_pct, r.opponent?.league_pct]));
  return (
    <div className="table-scroll">
      <table className="data pairing-table">
        <caption className="t-small ink-2">
          {subject} offense vs. {opponent} defense
        </caption>
        <thead>
          <tr>
            <th scope="col" className="sticky">Signal</th>
            <UnitHeads label={`${subject} off`} showPct={showPct} />
            <UnitHeads label={`${opponent} def`} showPct={showPct} />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${subjectLabel(r.subject)}-${r.base}`}>
              <th scope="row" className="sticky">
                {signalLabel(r.base)}
                <span className="raw-name mono t-small ink-3"> {r.base}</span>
              </th>
              <UnitCells u={r.subject} showPct={showPct} />
              <UnitCells u={r.opponent} showPct={showPct} />
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
