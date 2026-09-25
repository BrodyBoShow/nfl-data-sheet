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

const etClock = new Intl.DateTimeFormat("en-US", {
  timeZone: ET,
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
const etDate = new Intl.DateTimeFormat("en-CA", {
  timeZone: ET,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

/** A UTC instant as ET clock time, "15:28". */
export function formatEtClock(iso: string): string {
  return etClock.format(new Date(iso));
}

/** A UTC instant's ET calendar day, "2026-09-24". */
export function etDay(t: Date | string): string {
  return etDate.format(typeof t === "string" ? new Date(t) : t);
}

const WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"] as const;

/** An ET calendar day as given by games.gameday ("2026-09-24"), shown as "THU 09-24".
 *  Pure date arithmetic, no time zone involved. */
export function formatGameday(gameday: string): string {
  const d = new Date(`${gameday}T12:00:00Z`);
  return `${WEEKDAYS[d.getUTCDay()]} ${gameday.slice(5)}`;
}

/** A sourced line (market spread or total) exactly as captured. Halves show one
 *  decimal and quarter-point medians two, so 2.25 is never rounded to 2.3. */
export function formatLine(x: number): string {
  return Number.isInteger(x * 2) ? formatFixed(x, 1) : formatFixed(x, 2);
}

/** A home-negative spread labeled by the favored team: −4.5 with GB at home →
 *  "GB −4.5"; +2.5 → the away team, "CAR −2.5"; a value that shows as zero → "PK".
 *  `model` caps precision at one decimal (P6.md §5); market lines keep theirs. */
export function formatSpread(
  spreadHome: number,
  home: string,
  away: string,
  kind: "market" | "model",
): string {
  const favored = favoredTeam(spreadHome, home, away, kind);
  if (favored === null) return "PK";
  return `${favored} ${MINUS}${spreadMagnitude(spreadHome, kind)}`;
}

function spreadMagnitude(spreadHome: number, kind: "market" | "model"): string {
  return kind === "model" ? formatFixed(Math.abs(spreadHome), 1) : formatLine(Math.abs(spreadHome));
}

/** The team a line favors as displayed, or null for a pick'em (the value shows as zero).
 *  formatSpread and the favorite-flip marker both use this, so the team label and the
 *  marker can never disagree. */
export function favoredTeam(
  spreadHome: number,
  home: string,
  away: string,
  kind: "market" | "model",
): string | null {
  if (Number(spreadMagnitude(spreadHome, kind)) === 0) return null;
  return spreadHome < 0 ? home : away;
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
