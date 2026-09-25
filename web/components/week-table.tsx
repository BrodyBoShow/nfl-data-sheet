import Link from "next/link";

import { FlipMarker } from "@/components/marks";
import { formatFixed, formatLine, formatSpread } from "@/lib/format";
import type { RowStatus, WeekDay, WeekRow } from "@/lib/week";

// Week view table (docs/phases/P6.md §4). Market and model lines sit side by side. There
// is no edge column and nothing is sortable (§5, §8 Q1). A projection always appears
// with its stability bucket in the same row.

const ROOF: Record<number, string> = {
  1: "dome",
  2: "retractable, closed",
  3: "retractable (if roof open)",
  4: "open air",
};

const DASH = <span className="null">—</span>;

function Status({ s }: { s: RowStatus }) {
  switch (s.kind) {
    case "locked":
      return <span title="Projection fixed at this time (ET); it never changes after the lock">LOCKED {s.at}</span>;
    case "provisional":
      return (
        <span title="Projection can still change until the lock window opens">
          PROVISIONAL · locks {s.locksFrom}
        </span>
      );
    case "not_locked":
      return <span className="warn">NOT LOCKED · kicked off without a lock</span>;
    case "not_projected":
      return <span className="warn">{s.label}</span>;
    case "no_card":
      return <span className={s.warn ? "warn" : "ink-3"}>{s.reason}</span>;
  }
}

function Venue({ v }: { v: WeekRow["venue"] }) {
  const roof = v.roofCode !== null ? ROOF[v.roofCode] : undefined;
  const parts: string[] = [];
  if (v.weatherStatus === 1) {
    if (v.temperatureF !== null) parts.push(`${Math.round(v.temperatureF)}°F`);
    if (v.windMph !== null) parts.push(`outside wind ${Math.round(v.windMph)} mph`);
  } else if (v.weatherStatus === 3 && !v.kickedOff) {
    parts.push("forecast pending");
  } else if (v.weatherStatus === 4) {
    parts.push("forecast missed");
  } else if (v.weatherStatus === 5) {
    parts.push("venue unresolved");
  }
  if (!roof && parts.length === 0) return DASH;
  return <>{[roof, ...parts].filter(Boolean).join(" · ")}</>;
}

function Row({ r }: { r: WeekRow }) {
  const p = r.projection;
  return (
    <tr>
      <th scope="row" className="sticky mono">
        <Link href={`/game/${r.gameId}`}>
          {r.away} {r.neutral ? "vs" : "@"} {r.home}
        </Link>
      </th>
      <td className="num">{r.gametime ?? DASH}</td>
      <td className="num">
        {r.market.spreadHome !== null ? formatSpread(r.market.spreadHome, r.home, r.away, "market") : DASH}
      </td>
      <td className="num">
        {p ? (
          <>
            {r.favoriteFlipped ? (
              <>
                <FlipMarker />{" "}
              </>
            ) : null}
            {formatSpread(p.spreadHome, r.home, r.away, "model")}
          </>
        ) : (
          DASH
        )}
      </td>
      <td className="num">{r.market.total !== null ? formatLine(r.market.total) : DASH}</td>
      <td className="num">{p ? formatFixed(p.total, 1) : DASH}</td>
      <td
        className="t-cap"
        title={
          p
            ? `Minimum input stability ${formatFixed(p.stabilityMin, 2)} (${p.bucket} bucket). Not confidence in the result.`
            : undefined
        }
      >
        {p ? p.bucket.toUpperCase() : DASH}
      </td>
      <td className="t-small">
        <Status s={r.status} />
      </td>
      <td className="t-small">
        <Venue v={r.venue} />
      </td>
    </tr>
  );
}

/** If no game this week has a card and they all share one reason, return that reason.
 *  The week then renders as a plain schedule, with the reason stated once rather than
 *  on every row. */
export function sharedNoCardReason(days: WeekDay[]): { reason: string; warn: boolean } | null {
  const statuses = days.flatMap((d) => d.rows.map((r) => r.status));
  const first = statuses[0];
  if (!first || first.kind !== "no_card") return null;
  const same = statuses.every((s) => s.kind === "no_card" && s.reason === first.reason);
  return same ? { reason: first.reason, warn: first.warn } : null;
}

function ScheduleTable({ days, reason }: { days: WeekDay[]; reason: { reason: string; warn: boolean } }) {
  return (
    <>
      <p className={`legend t-small ${reason.warn ? "warn" : "ink-2"}`}>
        {reason.reason.replace(/^no card/, "No cards this week")}.
      </p>
      <div className="table-scroll">
        <table className="data week-table schedule">
          <thead>
            <tr>
              <th scope="col" className="sticky">Matchup</th>
              <th scope="col" className="num">Kickoff ET</th>
            </tr>
          </thead>
          {days.map((d) => (
            <tbody key={d.gameday ?? d.label}>
              <tr className="group">
                <th colSpan={2} scope="rowgroup">
                  <span className="group-label">{d.label}</span>
                </th>
              </tr>
              {d.rows.map((r) => (
                <tr key={r.gameId}>
                  <th scope="row" className="sticky mono">
                    <Link href={`/game/${r.gameId}`}>
                      {r.away} {r.neutral ? "vs" : "@"} {r.home}
                    </Link>
                  </th>
                  <td className="num">{r.gametime ?? DASH}</td>
                </tr>
              ))}
            </tbody>
          ))}
        </table>
      </div>
    </>
  );
}

export function WeekTable({ days }: { days: WeekDay[] }) {
  const shared = sharedNoCardReason(days);
  if (shared) return <ScheduleTable days={days} reason={shared} />;
  const anyWeather = days.some((d) => d.rows.some((r) => r.venue.weatherStatus === 1));
  return (
    <>
      <div className="table-scroll">
        <table className="data week-table">
          <thead>
            <tr>
              <th scope="col" className="sticky">Matchup</th>
              <th scope="col" className="num">Kickoff ET</th>
              <th scope="col" className="num">Market spread</th>
              <th scope="col" className="num">Model spread</th>
              <th scope="col" className="num">Market total</th>
              <th scope="col" className="num">Model total</th>
              <th scope="col">Stab</th>
              <th scope="col">Status</th>
              <th scope="col">Venue</th>
            </tr>
          </thead>
          {days.map((d) => (
            <tbody key={d.gameday ?? d.label}>
              <tr className="group">
                <th colSpan={9} scope="rowgroup">
                  <span className="group-label">{d.label}</span>
                </th>
              </tr>
              {d.rows.map((r) => (
                <Row key={r.gameId} r={r} />
              ))}
            </tbody>
          ))}
        </table>
      </div>
      <p className="legend t-small ink-2">
        Market: latest pre-kickoff consensus (median across books). Model: the synthesizer&apos;s
        projection, fixed at the lock for LOCKED rows. Stab: stability of the projection&apos;s
        inputs, not confidence in the result. Times ET.
      </p>
      {anyWeather ? (
        <p className="legend t-small ink-2">
          Weather data by{" "}
          <a href="https://open-meteo.com/" rel="noopener">
            Open-Meteo.com
          </a>{" "}
          (CC BY 4.0). Wind is an outside 10 m estimate, not wind at the field.
        </p>
      ) : null}
    </>
  );
}
