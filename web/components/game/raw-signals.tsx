import type { SignalRowT } from "@/lib/db";
import { formatEtStamp, formatFixed } from "@/lib/format";
import { signalLabel } from "@/lib/signal-labels";

// Raw signal rows (docs/phases/P6.md §6, item 5): Q6 as a flat table. These are the
// table's current values. A card is frozen at its last change, so they may differ.

const DASH = <span className="null">—</span>;

export function RawSignals({ rows, open = false }: { rows: SignalRowT[]; open?: boolean }) {
  return (
    <details className="section raw-signals" open={open}>
      <summary className="section-label t-cap">Raw signal rows ({rows.length})</summary>
      <p className="legend t-small ink-2">
        Current values in the signals table. The card above is frozen at its last change and may
        differ.
      </p>
      <div className="table-scroll">
        <table className="data raw-table">
          <thead>
            <tr>
              <th scope="col">Sector</th>
              <th scope="col">Team</th>
              <th scope="col">Signal</th>
              <th scope="col" className="num">Value</th>
              <th scope="col" className="num">n</th>
              <th scope="col" className="num">Stab</th>
              <th scope="col">As of (ET)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.sector}-${r.team ?? ""}-${r.game_id ?? ""}-${r.signal}`}>
                <td>{r.sector}</td>
                <td className="mono">{r.team ?? DASH}</td>
                <th scope="row">
                  {signalLabel(r.signal)}
                  {signalLabel(r.signal) !== r.signal ? (
                    <span className="raw-name mono t-small ink-3"> {r.signal}</span>
                  ) : null}
                </th>
                <td className="num">{r.value === null ? DASH : formatFixed(r.value, 3)}</td>
                <td className="num">{r.sample_n ?? DASH}</td>
                <td className="num">{r.stability === null ? DASH : formatFixed(r.stability, 2)}</td>
                <td className="mono t-small">{formatEtStamp(new Date(r.as_of)).replace(" ET", "")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
