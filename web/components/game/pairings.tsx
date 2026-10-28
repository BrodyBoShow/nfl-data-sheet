import type { CardBrief, CardPairing } from "@/lib/card";
import { HEADLINE_PAIRING_BASES, anyLeaguePct } from "@/lib/game";
import { signalLabel } from "@/lib/signal-labels";

import { UnitCells, UnitHeads } from "./signal-cells";

// Efficiency pairings (docs/phases/P6.md §6, context, not used by the model). The rows
// are the card's own pair_unit_signals output: a subject unit's `_off` signal beside
// the opponent's matching `_def`.
//
// The headline rows (HEADLINE_PAIRING_BASES, docs/phases/P11.md decision 2) are open; the
// rest collapse under them, counted. The defense column says "allowed": a defense's EPA
// per play is what it allows, so higher is worse for it (P11 C3).
//
// P7 slot: `subjectLabel` is the one place a row gets its name. It returns the team code
// now and would return a player name once player rows (player_id set) exist.
// The league-percentile column appears only once some brief in the table carries
// league_pct (see signal-cells.tsx).

export function subjectLabel(b: Pick<CardBrief, "team" | "player_id">): string {
  return b.team ?? b.player_id ?? "—";
}

const HEADLINE: readonly string[] = HEADLINE_PAIRING_BASES;

type Row = CardPairing & { model?: boolean };

function Table({
  rows,
  subject,
  opponent,
  showPct,
  caption,
  kind,
}: {
  rows: Row[];
  subject: string;
  opponent: string;
  showPct: boolean;
  caption: string;
  kind: "headline" | "more";
}) {
  const modelBases = new Set(rows.filter((r) => r.model).map((r) => r.base));
  return (
    <div className="table-scroll">
      <table className="data pairing-table" data-pairings={kind}>
        <caption className="t-small ink-2">{caption}</caption>
        <thead>
          <tr>
            <th scope="col" className="sticky">Signal</th>
            <UnitHeads label={`${subject} off`} showPct={showPct} />
            <UnitHeads label={`${opponent} def allowed`} showPct={showPct} />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${subjectLabel(r.subject)}-${r.base}`}>
              <th scope="row" className="sticky">
                {signalLabel(r.base)}
                {modelBases.has(r.base) ? <span className="t-small ink-2" data-model-input> · the model&apos;s input</span> : null}
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

/** `modelRows`: the model's inputs as rows (lib/game.ts, modelInputPairings), which the
 *  card's pairings leave out. Marked as the model's input where they show. */
export function PairingTable({
  rows: cardRows,
  modelRows = [],
  subject,
  opponent,
}: {
  rows: CardPairing[];
  modelRows?: CardPairing[];
  subject: string;
  opponent: string;
}) {
  const rows: Row[] = [...modelRows.map((r) => ({ ...r, model: true })), ...cardRows];
  if (rows.length === 0) {
    return (
      <p className="t-small ink-2">
        {subject} offense vs. {opponent} defense: no efficiency signals on the card for this week yet.
      </p>
    );
  }
  const showPct = anyLeaguePct(rows.flatMap((r) => [r.subject.league_pct, r.opponent?.league_pct]));
  const headline = HEADLINE.flatMap((b) => rows.filter((r) => r.base === b));
  const more = rows.filter((r) => !HEADLINE.includes(r.base));
  const caption = `${subject} offense vs. ${opponent} defense`;
  return (
    <div className="pairings" data-pairings-for={subject}>
      {headline.length ? (
        <Table rows={headline} subject={subject} opponent={opponent} showPct={showPct} caption={caption} kind="headline" />
      ) : null}
      {more.length ? (
        <details className="pairings-more" data-pairings-more>
          <summary className="t-small ink-2">
            {more.length} more {subject} offense vs. {opponent} defense {more.length === 1 ? "signal" : "signals"}
          </summary>
          <Table rows={more} subject={subject} opponent={opponent} showPct={showPct} caption={caption} kind="more" />
        </details>
      ) : null}
    </div>
  );
}
