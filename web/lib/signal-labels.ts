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

/** Which way is better for an offense, per root (docs/phases/P11.md, C3). A hardcoded
 *  vocabulary, so game.test.ts checks it both ways against the registry
 *  (docs/signals.md): every efficiency base resolves to a root here, and every root here
 *  is some base's. Pass/rush/down splits take their root's direction. A defense's value is
 *  what it allows, so the defense direction is the opposite. */
export const OFFENSE_BETTER: Record<string, "high" | "low"> = {
  epa_per_play: "high",
  success_rate: "high",
  explosive_rate: "high",
  points_per_drive: "high",
  three_and_out_rate: "low",
  red_zone_td_rate: "high",
};

/** The root an efficiency base belongs to ("success_rate_down3" → "success_rate"). */
export function efficiencyRoot(base: string): string | null {
  for (const root of Object.keys(OFFENSE_BETTER)) {
    if (base === root) return root;
    const rest = base.startsWith(`${root}_`) ? base.slice(root.length + 1) : null;
    if (rest === "pass" || rest === "rush" || /^down[1-4]$/.test(rest ?? "")) return root;
  }
  return null;
}

/** Which way is better for the unit a signal describes: "epa_per_play_off" → high,
 *  "epa_per_play_def" → low. null for a signal with no declared direction. */
export function goodDirection(signal: string): "high" | "low" | null {
  const side = /_(off|def)$/.exec(signal)?.[1];
  const root = efficiencyRoot(signalBase(signal));
  if (!side || !root) return null;
  const off = OFFENSE_BETTER[root]!;
  return side === "off" ? off : off === "high" ? "low" : "high";
}

/** "better" / "worse" when a percentile is in that third of the league for its unit,
 *  oriented by goodDirection. null in the middle third, or without a direction. */
export function pctThird(signal: string, pct: number | null | undefined): "better" | "worse" | null {
  const dir = goodDirection(signal);
  if (dir === null || pct == null) return null;
  const oriented = dir === "high" ? pct : 100 - pct;
  if (oriented >= 200 / 3) return "better";
  if (oriented <= 100 / 3) return "worse";
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
