// Display names and number formats for signals (docs/phases/P6.md §3, §4). An unknown
// signal falls back to its raw name and plain formatting, so P7 signals render without
// a code change. Formatting only, no arithmetic on values except the %-scaling of rates.
import { formatFixed } from "./format";

type Kind = "epa" | "rate" | "per_drive";

const DOWNS: Record<string, string> = { "1": "1st down", "2": "2nd down", "3": "3rd down", "4": "4th down" };

// The 20 efficiency bases (docs/signals.md registry). _off/_def is the unit, shown in the
// table header rather than in every label.
function efficiencyBase(base: string): { label: string; kind: Kind } | null {
  const table: Record<string, [string, Kind]> = {
    epa_per_play: ["EPA/play", "epa"],
    success_rate: ["Success rate", "rate"],
    explosive_rate: ["Explosive-play rate", "rate"],
    points_per_drive: ["Points/drive", "per_drive"],
    three_and_out_rate: ["Three-and-out rate", "rate"],
    red_zone_td_rate: ["Red-zone TD rate", "rate"],
  };
  for (const [root, [label, kind]] of Object.entries(table)) {
    if (base === root) return { label, kind };
    const rest = base.startsWith(`${root}_`) ? base.slice(root.length + 1) : null;
    if (rest === "pass" || rest === "rush") return { label: `${label}, ${rest}`, kind };
    const down = rest?.match(/^down([1-4])$/)?.[1];
    if (down) return { label: `${label}, ${DOWNS[down]}`, kind };
  }
  return null;
}

/** "epa_per_play_def" → "epa_per_play". */
export function signalBase(signal: string): string {
  return signal.replace(/_(off|def)$/, "");
}

export function signalLabel(signal: string): string {
  return efficiencyBase(signalBase(signal))?.label ?? signal;
}

/** EPA to 3 decimals; rates as % to 1 decimal; points/drive to 2; unknown signals to 3. */
export function formatSignalValue(signal: string, value: number | null): string | null {
  if (value === null) return null;
  const kind = efficiencyBase(signalBase(signal))?.kind;
  if (kind === "rate") return `${formatFixed(value * 100, 1)}%`;
  if (kind === "per_drive") return formatFixed(value, 2);
  return formatFixed(value, 3);
}
