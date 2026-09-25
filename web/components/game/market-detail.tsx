import type { Card } from "@/lib/card";
import { formatFixed, formatLine, formatSpread } from "@/lib/format";

// Market detail (docs/phases/P6.md §6, context). Descriptive only: consensus lines and
// how they moved, from the Market signals on the card (docs/signals.md, Market sector).
// Not used by the model.

const DASH = <span className="null">—</span>;
const show = (s: string | null | undefined) => (s === null || s === undefined ? DASH : s);

const STATUS: Record<number, string> = {
  1: "movement available (an own-week open plus a later capture)",
  2: "one own-week capture: the latest line is also the open",
  3: "lookahead captures only (no own-week line)",
  4: "awaiting a capture",
  5: "missed: nothing captured before kickoff",
};
const OPEN_BASIS: Record<number, string> = {
  1: "the week's opener, captured on time",
  2: "the week's opener, captured late",
  3: "no opener line; open is a later own-week capture",
};

/** A home-negative spread move as distance and direction, e.g. "1.5 toward GB". */
function spreadMove(x: number | null, home: string, away: string): string | null {
  if (x === null) return null;
  const shown = formatLine(Math.abs(x));
  if (Number(shown) === 0) return "none";
  return `${shown} toward ${x < 0 ? home : away}`;
}

function totalMove(x: number | null): string | null {
  if (x === null) return null;
  const shown = formatLine(Math.abs(x));
  if (Number(shown) === 0) return "none";
  return `${shown} ${x > 0 ? "higher" : "lower"}`;
}

export function MarketDetail({ card }: { card: Card }) {
  const m = card.market;
  const v = (k: string) => m.signals[k]?.value ?? null;
  const { home_team: home, away_team: away } = card.identity;
  const spread = (x: number | null) => (x === null ? null : formatSpread(x, home, away, "market"));
  const line = (x: number | null) => (x === null ? null : formatLine(x));
  const yesNo = (x: number | null) => (x === null ? null : x === 1 ? "yes" : "no");
  const books = (a: number | null, b: number | null) =>
    a === null && b === null ? null : `${a ?? "—"} → ${b ?? "—"}`;
  const hours = (x: number | null) => (x === null ? null : `${formatFixed(x, 1)} h`);
  const straddleNote = card.edge.vs_current.flag_notes.spread_key_straddle;
  const status = m.status;

  const rows: [string, string | null, string | null][] = [
    ["Open", spread(v("spread_home_open")), line(v("total_open"))],
    ["Latest", spread(v("spread_home_current")), line(v("total_current"))],
    ["Move, open → latest", spreadMove(v("spread_home_move"), home, away), totalMove(v("total_move"))],
    ["Move per day", spreadMove(v("spread_move_per_day"), home, away), totalMove(v("total_move_per_day"))],
    ["Book range (latest)", line(v("spread_book_range")), line(v("total_book_range"))],
    ["Books, open → latest", books(v("spread_book_count_open"), v("spread_book_count_current")),
      books(v("total_book_count_open"), v("total_book_count_current"))],
    ["Book set changed", yesNo(v("spread_book_set_changed")), yesNo(v("total_book_set_changed"))],
    ["Key numbers crossed", v("spread_key_crossings") === null ? null : String(v("spread_key_crossings")), null],
    ["Books straddle a key number", yesNo(v("spread_key_straddle")), null],
  ];

  const teams = [away, home];
  const teamVal = (t: string, k: string) => m.teams[t]?.[k]?.value ?? null;

  return (
    <section className="section" aria-labelledby="market-h">
      <h2 id="market-h" className="section-label t-cap">
        Market detail · not used by the model
      </h2>
      <p className="t-small ink-2">
        Status: {status === null ? "no market rows" : STATUS[status] ?? `code ${status}`}
        {v("market_open_basis") !== null ? ` · open: ${OPEN_BASIS[v("market_open_basis")!] ?? "—"}` : ""}
        {` · own-week captures: ${v("market_own_week_captures") ?? "—"}`}
        {v("market_open_lead_hours") !== null ? ` · open captured ${hours(v("market_open_lead_hours"))} before kickoff` : ""}
        {v("market_current_lead_hours") !== null ? ` · latest ${hours(v("market_current_lead_hours"))} before kickoff` : ""}
      </p>
      <div className="table-scroll">
        <table className="data market-table">
          <thead>
            <tr>
              <th scope="col">Consensus (median across books)</th>
              <th scope="col" className="num">Spread</th>
              <th scope="col" className="num">Total</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([label, s, t]) => (
              <tr key={label}>
                <th scope="row">{label}</th>
                <td className="num">{show(s)}</td>
                <td className="num">{show(t)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {straddleNote && v("spread_key_straddle") === 1 ? <p className="legend t-small ink-2">{straddleNote}</p> : null}
      <p className="legend t-small ink-2">
        A move can come from books joining or leaving (see book set changed), not only from books
        moving.
      </p>
      <div className="table-scroll">
        <table className="data market-implied-table">
          <caption className="t-small ink-2">Market-implied, from the latest consensus</caption>
          <thead>
            <tr>
              <th scope="col">Team</th>
              <th scope="col" className="num">Implied points</th>
              <th scope="col" className="num">Win probability (no-vig)</th>
            </tr>
          </thead>
          <tbody>
            {teams.map((t) => {
              const pts = teamVal(t, "implied_team_total");
              const wp = teamVal(t, "win_prob_novig");
              return (
                <tr key={t}>
                  <th scope="row" className="mono">{t}</th>
                  <td className="num">{pts === null ? DASH : formatLine(pts)}</td>
                  <td className="num">{wp === null ? DASH : `${formatFixed(wp * 100, 1)}%`}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
