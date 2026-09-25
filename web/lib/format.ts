// Display formatting. Formatting only: nothing here computes a metric
// (docs/phases/P6.md §4, CLAUDE.md layer rules).

const ET = "America/New_York";

const etStamp = new Intl.DateTimeFormat("en-US", {
  timeZone: ET,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/** "2026-09-24 15:28 ET". Absolute time only: pages are ISR-cached, so a relative time
 *  ("in 3h") would go stale. */
export function formatEtStamp(t: Date): string {
  const p = Object.fromEntries(etStamp.formatToParts(t).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute} ET`;
}

const MINUS = "−";

/** Fixed decimals, true minus sign (U+2212). A value that rounds to zero shows as
 *  unsigned zero, never "−0.00". */
export function formatFixed(x: number, decimals: number): string {
  const s = x.toFixed(decimals);
  if (Number(s) === 0) return (0).toFixed(decimals);
  return s.startsWith("-") ? MINUS + s.slice(1) : s;
}

/** "[−0.08, 0.02]" */
export function formatInterval([lo, hi]: readonly [number, number], decimals: number): string {
  return `[${formatFixed(lo, decimals)}, ${formatFixed(hi, decimals)}]`;
}

const count = new Intl.NumberFormat("en-US");

/** "1,615" */
export function formatCount(n: number): string {
  return count.format(n);
}

/** [2020, …, 2025] → "2020–25". Non-contiguous seasons are listed, never collapsed. */
export function formatSeasonRange(seasons: readonly number[]): string {
  if (seasons.length === 0) return "";
  const first = seasons[0]!;
  const last = seasons[seasons.length - 1]!;
  const contiguous = seasons.every((s, i) => s === first + i);
  if (!contiguous) return seasons.join(", ");
  if (first === last) return String(first);
  return `${first}–${String(last).slice(-2)}`;
}
