import Link from "next/link";

import type { Week } from "@/lib/db";

// Season and week navigation from Q1. Every season in the schedule is linked,
// including pre-2026 seasons with no cards (§8 Q6).
export function WeekNav({ weeks, season, week }: { weeks: Week[]; season: number; week: number }) {
  const seasons = [...new Set(weeks.map((w) => w.season))];
  const inSeason = weeks.filter((w) => w.season === season);
  const at = weeks.findIndex((w) => w.season === season && w.week === week);
  const prev = at > 0 ? weeks[at - 1] : undefined;
  const next = at >= 0 && at < weeks.length - 1 ? weeks[at + 1] : undefined;
  const href = (w: Week) => `/${w.season}/${w.week}`;

  return (
    <nav className="week-nav t-small mono" aria-label="Season and week">
      <div className="nav-row">
        <span className="t-cap ink-2">Season</span>
        {seasons.map((s) => {
          const first = weeks.find((w) => w.season === s)!;
          return (
            <Link key={s} href={href(first)} aria-current={s === season ? "page" : undefined}>
              {s}
            </Link>
          );
        })}
      </div>
      <div className="nav-row">
        <span className="t-cap ink-2">Week</span>
        {inSeason.map((w) => (
          <Link
            key={w.week}
            href={href(w)}
            aria-current={w.week === week ? "page" : undefined}
            title={`${w.n_games} games · ${w.n_cards} cards`}
          >
            {w.week}
          </Link>
        ))}
      </div>
      <div className="nav-row">
        {prev ? <Link href={href(prev)}>← {prev.season} wk {prev.week}</Link> : <span />}
        {next ? <Link href={href(next)}>{next.season} wk {next.week} →</Link> : null}
      </div>
    </nav>
  );
}
