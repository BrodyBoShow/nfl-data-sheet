import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { Arithmetic } from "@/components/game/arithmetic";
import { Availability } from "@/components/game/availability";
import { Environment } from "@/components/game/environment";
import { Lines } from "@/components/game/lines";
import { MarketDetail } from "@/components/game/market-detail";
import { PairingTable } from "@/components/game/pairings";
import { RawSignals } from "@/components/game/raw-signals";
import { LowStabilityNote } from "@/components/game/signal-cells";
import { TeamSignals } from "@/components/game/team-signals";
import { backtest } from "@/lib/backtest";
import type { Card } from "@/lib/card";
import { card as loadCard, game, gameSignals, isGameId } from "@/lib/db";
import { etDay, formatEtClock, formatEtStamp, formatGameday } from "@/lib/format";
import { buildLines, gameStatus, isLowStability, type GameStatus } from "@/lib/game";
import { normalizeTeam } from "@/lib/teams";
import { noCardReason } from "@/lib/week";

// Game view (docs/phases/P6.md §6). Time-based ISR, rendered on first request.
export const revalidate = 600;
export async function generateStaticParams() {
  return [];
}

type Params = Promise<{ gameId: string }>;

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const { gameId } = await params;
  if (!isGameId(gameId)) return { title: "Not found" };
  const [, , away, home] = gameId.split("_");
  return { title: `${away} at ${home}, ${gameId.slice(0, 4)} week ${Number(gameId.slice(5, 7))}` };
}

function StatusText({ s }: { s: GameStatus }) {
  switch (s.kind) {
    case "locked":
      return (
        <span>
          LOCKED {s.at} ET{s.leadHours ? `, ${s.leadHours} h before kickoff` : ""}
        </span>
      );
    case "provisional":
      return <span>PROVISIONAL{s.locksFrom ? ` · locks from ${s.locksFrom} ET` : ""}</span>;
    case "not_locked":
      return <span className="warn">NOT LOCKED · kicked off without a lock</span>;
    case "not_projected":
      return <span className="warn">{s.label}</span>;
  }
}

function CardBody({ card, asOf, now }: { card: Card; asOf: string; now: Date }) {
  const lines = buildLines(card);
  const status = gameStatus(card, now);
  const kickedOff = new Date(card.identity.kickoff) <= now;
  const { home_team: home, away_team: away } = card.identity;
  const pairs = card.context.efficiency_pairings;
  return (
    <>
      <p className="t-small mono">
        <StatusText s={status} />
        <span className="ink-2">
          {" · card last changed "}
          <span className="nowrap">{formatEtStamp(new Date(asOf))}</span>
        </span>
        {card.identity.outside_fit_scope ? (
          <span className="warn"> · postseason: outside the model&apos;s fit (regular season only)</span>
        ) : null}
      </p>
      <div className="game-top">
        <div>
          {lines ? (
            <Lines v={lines} />
          ) : (
            <section className="section">
              <h2 className="section-label t-cap">Lines</h2>
              <p className="warn">Not projected: {card.projection_status_label}.</p>
            </section>
          )}
        </div>
        <div>
          <Environment card={card} kickedOff={kickedOff} />
          <Availability card={card} />
        </div>
      </div>
      {lines && card.projection ? <Arithmetic card={card} projection={card.projection} /> : null}
      <section className="section" aria-labelledby="pairings-h">
        <h2 id="pairings-h" className="section-label t-cap">
          Efficiency matchups · not used by the model
        </h2>
        <p className="legend t-small ink-2">
          Each offense rating beside the opposing defense&apos;s matching rating, entering this week.
          The model uses EPA/play only (above).
        </p>
        <PairingTable rows={pairs.home_offense} subject={home} opponent={away} />
        <PairingTable rows={pairs.away_offense} subject={away} opponent={home} />
        <LowStabilityNote
          show={[...pairs.home_offense, ...pairs.away_offense].some((r) =>
            [r.subject, r.opponent].some((b) => b != null && b.value !== null && isLowStability(b.stability)),
          )}
        />
      </section>
      <MarketDetail card={card} />
    </>
  );
}

export default async function GamePage({ params }: { params: Params }) {
  const { gameId } = await params;
  if (!isGameId(gameId)) notFound();
  const g = await game(gameId);
  if (!g) notFound();

  const [rec, rows] = await Promise.all([
    loadCard(gameId),
    gameSignals(g.season, g.week, g.game_id, normalizeTeam(g.home_team), normalizeTeam(g.away_team)),
  ]);
  const now = new Date();
  const neutral = g.location === "Neutral";
  const parsed = rec?.card;
  const kickoff =
    parsed?.ok === true
      ? `${formatGameday(etDay(parsed.card.identity.kickoff))} ${formatEtClock(parsed.card.identity.kickoff)} ET`
      : `${g.gameday ? formatGameday(g.gameday) : "date TBD"} ${g.gametime ?? ""} ET`;

  return (
    <>
      <div className="page-head">
        <h1 className="t-title mono">
          {g.away_team} {neutral ? "vs" : "@"} {g.home_team}
        </h1>
        <p className="t-small mono ink-2">
          <Link href={`/${g.season}/${g.week}`}>
            {g.season} · WK {g.week}
          </Link>{" "}
          · {kickoff} · {neutral ? "neutral site" : `at ${g.home_team}`}
        </p>
      </div>

      {parsed?.ok === true ? (
        <CardBody card={parsed.card} asOf={rec!.as_of} now={now} />
      ) : parsed ? (
        <section className="section">
          <h2 className="section-label t-cap">Card</h2>
          <p className="warn">
            {parsed.reason === "unsupported_version"
              ? `Card format not supported (card_version ${String(parsed.version)}). Showing the raw signals instead.`
              : "This card failed validation, so its numbers aren't shown. Showing the raw signals instead."}
          </p>
        </section>
      ) : (
        <p className="t-small ink-2">
          {
            noCardReason(g, { now, today: etDay(now), fitSeasons: backtest.fit_seasons }).reason.replace(
              /^no card/,
              "No card for this game",
            )
          }
          .
        </p>
      )}

      {parsed?.ok !== true ? (
        rows.some((r) => r.sector === "efficiency") ? (
          <TeamSignals rows={rows} home={normalizeTeam(g.home_team)} away={normalizeTeam(g.away_team)} />
        ) : (
          <p className="t-small ink-2">
            {g.season < 2019
              ? "No signals for this game. Efficiency signals start in 2019."
              : "No signals for this week yet."}
          </p>
        )
      ) : null}

      {rows.length ? <RawSignals rows={rows} /> : null}
    </>
  );
}
