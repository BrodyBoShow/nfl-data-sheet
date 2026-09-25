import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { game, isGameId } from "@/lib/db";

// Step 5 placeholder so week-view links resolve. Step 6 replaces it with the game view
// (docs/phases/P6.md §6).
export const revalidate = 600;
export async function generateStaticParams() {
  return [];
}

type Params = Promise<{ gameId: string }>;

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const { gameId } = await params;
  return { title: isGameId(gameId) ? gameId : "Not found" };
}

export default async function GamePage({ params }: { params: Params }) {
  const { gameId } = await params;
  if (!isGameId(gameId)) notFound();
  const g = await game(gameId);
  if (!g) notFound();
  return (
    <>
      <div className="page-head">
        <h1 className="t-title mono">
          {g.away_team} {g.location === "Neutral" ? "vs" : "@"} {g.home_team}
        </h1>
        <p className="t-small mono ink-2">
          {g.season} · WK {g.week} · {g.gameday ?? "date TBD"} {g.gametime ?? ""} ET
        </p>
      </div>
      <section className="section">
        <h2 className="section-label t-cap">Status</h2>
        <p className="ink-2">
          The game view is built in step 6.{" "}
          <Link href={`/${g.season}/${g.week}`}>Back to week {g.week}</Link>
        </p>
      </section>
    </>
  );
}
