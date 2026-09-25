import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { WeekNav } from "@/components/week-nav";
import { WeekTable } from "@/components/week-table";
import { backtest } from "@/lib/backtest";
import { listWeeks, weekCards, weekGames } from "@/lib/db";
import { etDay, formatEtStamp } from "@/lib/format";
import { buildWeek } from "@/lib/week";

// Time-based ISR (P6.md §1). Returning [] from generateStaticParams renders each week
// on first request and caches it; this is required for runtime ISR on dynamic routes
// (Next 16 docs).
export const revalidate = 600;
export async function generateStaticParams() {
  return [];
}

type Params = Promise<{ season: string; week: string }>;

function parse(season: string, week: string): { season: number; week: number } | null {
  if (!/^\d{4}$/.test(season) || !/^\d{1,2}$/.test(week)) return null;
  return { season: Number(season), week: Number(week) };
}

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const { season, week } = await params;
  const p = parse(season, week);
  return { title: p ? `${p.season} week ${p.week}` : "Not found" };
}

export default async function WeekPage({ params }: { params: Params }) {
  const { season: s, week: w } = await params;
  const p = parse(s, w);
  if (!p) notFound();

  const weeks = await listWeeks();
  const meta = weeks.find((x) => x.season === p.season && x.week === p.week);
  if (!meta) notFound();

  const [games, cards] = await Promise.all([weekGames(p.season, p.week), weekCards(p.season, p.week)]);
  const now = new Date();
  const days = buildWeek(games, cards, { now, today: etDay(now), fitSeasons: backtest.fit_seasons });
  const cardsAsOf = cards.map((c) => c.as_of).sort().at(-1);

  return (
    <>
      <div className="page-head">
        <h1 className="t-title">
          {p.season} · Week {p.week}
        </h1>
        <p className="t-small mono ink-2">
          {games.length} games · {cards.length} cards
          {cardsAsOf ? (
            <>
              {" · cards last changed "}
              <span className="nowrap">{formatEtStamp(new Date(cardsAsOf))}</span>
            </>
          ) : null}
        </p>
      </div>
      <WeekNav weeks={weeks} season={p.season} week={p.week} />
      <WeekTable days={days} />
    </>
  );
}
