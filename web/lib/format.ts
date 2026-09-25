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
