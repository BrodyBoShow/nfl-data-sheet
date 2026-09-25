import { backtest } from "@/lib/backtest";
import { formatFixed } from "@/lib/format";
import { isLowStability, lowStabilityTitle } from "@/lib/game";
import { formatSignalValue } from "@/lib/signal-labels";

// One unit's cells in an efficiency table: value · (league pct) · n · stab, always adjacent
// (P6.md §4). Shared by the card pairings and the no-card team table.
//
// - The league-percentile column renders only when some row in the table has a value
//   (`showPct`). league_pct is null everywhere today, so the column is absent, and it
//   appears once Efficiency fills it: a data change, not a layout change.
// - A value below the stability floor is dimmed to --ink-3, with a title saying why.
//   n and stab stay at full weight, since they are the reason.

const DASH = <span className="null">—</span>;

export interface UnitValue {
  signal: string;
  value: number | null;
  league_pct?: number | null;
  sample_n: number | null;
  stability: number | null;
}

export function UnitHeads({ label, showPct }: { label: string; showPct: boolean }) {
  return (
    <>
      <th scope="col" className="num">{label}</th>
      {showPct && (
        <th scope="col" className="num" title="League percentile, 0–100">
          Pct
        </th>
      )}
      <th scope="col" className="num">n</th>
      <th scope="col" className="num">Stab</th>
    </>
  );
}

/** One line under a table that dims anything, defining the grey. */
export function LowStabilityNote({ show }: { show: boolean }) {
  if (!show) return null;
  return (
    <p className="legend t-small ink-2" data-low-stability-note>
      Grey values: stability below {formatFixed(backtest.stability_floor, 2)}, the lowest input stability in the
      model&apos;s backtest. Mostly league average; see n and Stab.
    </p>
  );
}

export function UnitCells({ u, showPct }: { u: UnitValue | null | undefined; showPct: boolean }) {
  const shown = u ? formatSignalValue(u.signal, u.value) : null;
  const stability = u?.stability ?? null;
  const dim = shown !== null && stability !== null && isLowStability(stability);
  return (
    <>
      <td
        className={dim ? "num ink-3" : "num"}
        data-low-stability={dim || undefined}
        title={dim ? lowStabilityTitle(stability) : undefined}
      >
        {shown ?? DASH}
      </td>
      {showPct && (
        <td className="num" data-pct>
          {u?.league_pct == null ? DASH : formatFixed(u.league_pct, 0)}
        </td>
      )}
      <td className="num">{u?.sample_n ?? DASH}</td>
      <td className="num">{u?.stability == null ? DASH : formatFixed(u.stability, 2)}</td>
    </>
  );
}
