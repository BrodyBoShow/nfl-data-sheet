import Link from "next/link";

import type { PlayerEff, PlayerUsage } from "@/lib/db";
import {
  FAMILIES,
  FTN_FIRST_SEASON,
  HIST_TABLES,
  ROLE_GROUPS,
  TAGS,
  TACKLE_L4_MIN_DEF_SNAPS,
  defSnapShare,
  familyRows,
  headlineL4,
  formatPlayerValue,
  histRows,
  parseSpan,
  roleRows,
  type Col,
  type Family,
  type Fmt,
  type HistTable,
  type Tag,
} from "@/lib/players";

// Players section (docs/phases/P7.md step 9). Each player's latest row through the week
// before the game, from Q7/Q8. Not used by the model. Nothing here is dimmed: stability
// is shown as a number beside the values (user decision 2026-09-30).

const DASH = <span className="null">—</span>;

function Tags({ tags }: { tags: Tag[] }) {
  if (tags.length === 0) return null;
  return (
    <>
      {tags.map((t) => (
        <span key={t}>
          {" · "}
          <abbr className="src" title={TAGS[t].title} data-source={TAGS[t].source}>
            {t}
          </abbr>
        </span>
      ))}
    </>
  );
}

function Head({
  label,
  tags,
  title,
  sticky = false,
  num = true,
  colSpan,
  rowSpan,
}: {
  label: string;
  tags: Tag[];
  title?: string;
  sticky?: boolean;
  num?: boolean;
  colSpan?: number;
  rowSpan?: number;
}) {
  const cls = [num ? "num" : "", sticky ? "sticky" : ""].filter(Boolean).join(" ") || undefined;
  return (
    <th scope="col" className={cls} title={title} colSpan={colSpan} rowSpan={rowSpan} data-tags={tags.join(",")}>
      {label}
      <Tags tags={tags} />
    </th>
  );
}

function Value({ col, fmt, v, title }: { col: string; fmt: Fmt; v: number | null | undefined; title?: string }) {
  const shown = formatPlayerValue(fmt, v);
  return (
    <td className="num" data-col={col} title={shown === null ? title : undefined}>
      {shown ?? DASH}
    </td>
  );
}

function Identity({
  row,
  asOfWeek,
}: {
  row: { player_id: string; display_name: string | null; position: string | null; week: number };
  asOfWeek: number;
}) {
  const earlier = row.week < asOfWeek;
  return (
    <>
      <th scope="row" className="sticky" data-player={row.player_id}>
        {row.display_name ?? <span className="mono">{row.player_id}</span>}
      </th>
      <td className="mono">{row.position ?? DASH}</td>
      <td
        className={earlier ? "num ink-2" : "num"}
        data-last-week={row.week}
        title={earlier ? `Latest game through week ${asOfWeek}: week ${row.week}` : undefined}
      >
        {row.week}
      </td>
    </>
  );
}

const IDENTITY_HEADS = (
  <>
    <Head label="Player" tags={[]} sticky num={false} />
    <Head label="Pos" tags={[]} num={false} />
    <Head label="Last wk" tags={[]} title="The week of the player's latest game through the week before this one." />
  </>
);

// ---- Role ---------------------------------------------------------------------------------

function RoleTable({ rows, team, asOfWeek }: { rows: PlayerUsage[]; team: string; asOfWeek: number }) {
  if (rows.length === 0) return null;
  return (
    <div className="table-scroll">
      <table className="data players-table" data-family="role">
        <caption className="t-small ink-2">{team} role · shares of the team&apos;s plays</caption>
        <thead>
          <tr>
            <Head label="Player" tags={[]} sticky num={false} rowSpan={2} />
            <Head label="Pos" tags={[]} num={false} rowSpan={2} />
            <Head label="Last wk" tags={[]} rowSpan={2} title="The week of the player's latest game through the week before this one." />
            <Head label="Games" tags={[]} rowSpan={2} title="Games with a snap this season." />
            {ROLE_GROUPS.map((g) => (
              <Head key={g.season} label={g.label} tags={g.tags} title={g.title} colSpan={g.last ? 2 : 1} />
            ))}
          </tr>
          <tr>
            {ROLE_GROUPS.flatMap((g) =>
              g.last
                ? [
                    <Head key={`${g.season}-s`} label="Season" tags={g.tags} />,
                    <Head key={`${g.season}-l`} label="Last game" tags={g.tags} />,
                  ]
                : [<Head key={`${g.season}-s`} label="Season" tags={g.tags} title={g.title} />],
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.player_id} data-row={r.player_id}>
              <Identity row={r} asOfWeek={asOfWeek} />
              <Value col="usage_games_std" fmt="count" v={r.usage_games_std} />
              {ROLE_GROUPS.flatMap((g) => [
                <Value key={g.season} col={g.season} fmt="pct" v={r[g.season] as number | null} />,
                ...(g.last ? [<Value key={g.last} col={g.last} fmt="pct" v={r[g.last] as number | null} />] : []),
              ])}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---- Families -----------------------------------------------------------------------------

function FamilyTable({
  family,
  rows,
  team,
  asOfWeek,
  usage,
}: {
  family: Family;
  rows: PlayerEff[];
  team: string;
  asOfWeek: number;
  usage: PlayerUsage[];
}) {
  if (rows.length === 0) return null;
  const h = family.headline;
  // A league percentile column only when some row has one (P6.md §4). Defense has none
  // while Pro Football Reference's defensive rows stay gated (P7 step 7; open item 9 closed
  // without a source switch).
  const showPct = rows.some((r) => r[h.pct] != null);
  const isDefense = family.id === "defense";
  const approx = family.cols.filter((c) => c.approx);
  const head = (c: Col<PlayerEff>) => (
    <Head key={c.key} label={c.approx ? `${c.label} †` : c.label} tags={c.tags} title={c.title} />
  );
  return (
    <div className="players-family" data-family-wrap={family.id}>
      <div className="table-scroll">
        <table className="data players-table" data-family={family.id}>
          <caption className="t-small ink-2">
            {team} {family.title.toLowerCase()}
          </caption>
          <thead>
            <tr>
              {IDENTITY_HEADS}
              {isDefense ? <Head label="Def snap %" tags={["PFR"]} title="Share of the team's defensive snaps, season." /> : null}
              <Head label={family.sample.label} tags={family.sample.tags} title="Season to date." />
              <Head label={isDefense ? "Stab ‡" : "Stab"} tags={family.stability.tags} title={family.stability.title} />
              <Head label={h.label} tags={h.tags} title="Season to date, blended with last season and the league (Stab)." />
              {showPct ? <Head label="Pct" tags={h.tags} title="League percentile within the position group, 0–100." /> : null}
              <Head label="Last 4" tags={h.tags} title="Last 4 games played, raw (not blended). Read with its n." />
              <Head label="L4 n" tags={family.sample.tags} title="The last-4 sample." />
              {family.cols.map(head)}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.player_id} data-row={r.player_id}>
                <Identity row={r} asOfWeek={asOfWeek} />
                {isDefense ? <Value col="def_snap_share_std" fmt="pct" v={defSnapShare(r, usage)} /> : null}
                <Value col={family.sample.std} fmt="count" v={r[family.sample.std] as number | null} />
                <td className="num" data-col={family.stability.key} data-stab>
                  {formatPlayerValue("stab", r[family.stability.key] as number | null) ?? DASH}
                </td>
                <Value col={h.std} fmt={h.fmt} v={r[h.std] as number | null} />
                {showPct ? <Value col={h.pct} fmt="rank" v={r[h.pct] as number | null} /> : null}
                <Value col={h.l4} fmt={h.fmt} v={headlineL4(family, r)} />
                <Value col={family.sample.l4} fmt="count" v={r[family.sample.l4] as number | null} />
                {family.cols.map((c) => (
                  <Value key={c.key} col={c.key} fmt={c.fmt} v={r[c.key] as number | null} />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {approx.length ? (
        <p className="legend t-small ink-2">† {approx.map((c) => c.title).join(" ")}</p>
      ) : null}
      {isDefense && !showPct ? (
        <p className="legend t-small ink-2" data-not-ranked>
          Not ranked: Pro Football Reference has no defensive row for some games a player played, so these rates
          can&apos;t be compared fairly across players.
        </p>
      ) : null}
      {isDefense ? (
        <p className="legend t-small ink-2">
          ‡ Defense stability rests on tackles, counted over games with a Pro Football Reference defensive row.
          Rates are per 100 defensive snaps. Tackle rates need a minimum of defensive snaps to mean anything, so
          Last 4 is blank under {TACKLE_L4_MIN_DEF_SNAPS}.
        </p>
      ) : null}
    </div>
  );
}

// ---- Coverage history ---------------------------------------------------------------------

/** Rows whose span is FTN's to attribute (2023 on). Anything else isn't shown. */
function ftnRows(rows: PlayerEff[]): { shown: PlayerEff[]; withheld: number } {
  const shown = rows.filter((r) => {
    const seasons = parseSpan(r.hist_span);
    return seasons !== null && seasons.every((s) => s >= FTN_FIRST_SEASON);
  });
  return { shown, withheld: rows.length - shown.length };
}

function HistTableView({ table, rows }: { table: HistTable; rows: PlayerEff[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="table-scroll">
      <table className="data players-table" data-family={`hist-${table.id}`}>
        <caption className="t-small ink-2">{table.title}</caption>
        <thead>
          <tr>
            <Head label="Player" tags={[]} sticky num={false} />
            <Head label="Pos" tags={[]} num={false} />
            <Head label="Seasons" tags={["FTN"]} title="The seasons these values cover. Not this season." />
            <Head label={table.n.label} tags={["FTN"]} title="Dropbacks with a man or zone label behind these values." />
            {table.cols.map((c) => (
              <Head key={c.key} label={c.label} tags={c.tags} title={c.title} />
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.player_id} data-row={r.player_id}>
              <th scope="row" className="sticky" data-player={r.player_id}>
                {r.display_name ?? <span className="mono">{r.player_id}</span>}
              </th>
              <td className="mono">{r.position ?? DASH}</td>
              <td className="num" data-span>
                {r.hist_span}
              </td>
              <Value col={table.n.key} fmt="count" v={r[table.n.key] as number | null} />
              {table.cols.map((c) => (
                <Value key={c.key} col={c.key} fmt={c.fmt} v={r[c.key] as number | null} />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CoverageHistory({ eff, team }: { eff: PlayerEff[]; team: string }) {
  const tables = HIST_TABLES.map((t) => ({ table: t, ...ftnRows(histRows(eff, team, t)) }));
  const shown = tables.flatMap((t) => t.shown);
  const withheld = tables.reduce((n, t) => n + t.withheld, 0);
  if (shown.length === 0 && withheld === 0) return null;
  const spans = [...new Set(shown.map((r) => r.hist_span))].sort().join(", ");
  return (
    <details className="players-history" data-coverage-history>
      <summary className="t-cap ink-2">
        {team} coverage history · {spans || "none"} · not this season
      </summary>
      <p className="legend t-small ink-2">
        Past seasons only, from charting of each play&apos;s coverage. Never this season&apos;s behavior.
      </p>
      {tables.map(({ table, shown: rows }) => (
        <HistTableView key={table.id} table={table} rows={rows} />
      ))}
      {withheld ? (
        <p className="legend t-small warn">
          {withheld} row{withheld === 1 ? "" : "s"} not shown: seasons before {FTN_FIRST_SEASON} carry a different
          attribution.
        </p>
      ) : null}
      <p className="legend t-small ink-2" data-cc-by-sa>
        Coverage history is adapted from FTN Data via nflverse (
        <a href="https://github.com/nflverse/nflverse-data/releases/tag/pbp_participation" rel="noopener">
          participation charting
        </a>
        ), licensed{" "}
        <a href="https://creativecommons.org/licenses/by-sa/4.0/" rel="license noopener">
          CC BY-SA 4.0
        </a>
        . Modified: this site aggregated the play-level charting into per-player rates for each season shown. These
        values are shared under{" "}
        <a href="https://creativecommons.org/licenses/by-sa/4.0/" rel="license noopener">
          CC BY-SA 4.0
        </a>
        . Provided as is, without warranties (license section 5). Full statement:{" "}
        <Link href="/sources#ftn">sources</Link>.
      </p>
    </details>
  );
}

// ---- Section ------------------------------------------------------------------------------

function Block({
  offense,
  defense,
  usage,
  eff,
  asOfWeek,
}: {
  offense: string;
  defense: string;
  usage: PlayerUsage[];
  eff: PlayerEff[];
  asOfWeek: number;
}) {
  const [passing, rushing, receiving, def] = FAMILIES;
  return (
    <div className="players-block" data-offense={offense} data-defense={defense}>
      <h3 className="t-head players-block-head">
        {offense} offense · {defense} defense
      </h3>
      <RoleTable rows={roleRows(usage, offense)} team={offense} asOfWeek={asOfWeek} />
      {[passing!, rushing!, receiving!].map((f) => (
        <FamilyTable key={f.id} family={f} rows={familyRows(eff, offense, f)} team={offense} asOfWeek={asOfWeek} usage={usage} />
      ))}
      <FamilyTable family={def!} rows={familyRows(eff, defense, def!)} team={defense} asOfWeek={asOfWeek} usage={usage} />
      <CoverageHistory eff={eff} team={offense} />
    </div>
  );
}

/** `teamSection` names the section on this page that shows team efficiency ratings, for
 *  the garbage-time statement (docs/signals.md, "Player tables"). */
export function PlayersSection({
  usage,
  eff,
  home,
  away,
  asOfWeek,
  teamSection,
}: {
  usage: PlayerUsage[];
  eff: PlayerEff[];
  home: string;
  away: string;
  asOfWeek: number;
  teamSection: string | null;
}) {
  if (usage.length === 0 && eff.length === 0) return null;
  return (
    <section className="section players" aria-labelledby="players-h" data-players>
      <h2 id="players-h" className="section-label t-cap">
        Players · not used by the model
      </h2>
      <p className="legend t-small ink-2" data-garbage-time>
        Player rates include garbage time.{" "}
        {teamSection ? `The team ratings in ${teamSection} exclude it` : "Team efficiency ratings exclude it"}, so they
        cover different plays and aren&apos;t directly comparable.
      </p>
      <p className="legend t-small ink-2">
        Each player&apos;s latest game through week {asOfWeek}. Stab is the share of a season value that isn&apos;t
        league average (0–1); it applies to every season value in its row. Nothing is dimmed. Last-4 values are raw:
        read them with their n.
      </p>
      <p className="legend t-small ink-2" data-snap-statement>
        Games and last-4 windows count games with a snap, from Pro Football Reference snap counts (Sports Reference
        LLC), via nflverse. Columns marked PFR, NGS or FTN are those providers&apos; data (
        <Link href="/sources">sources</Link>).
      </p>
      <Block offense={home} defense={away} usage={usage} eff={eff} asOfWeek={asOfWeek} />
      <Block offense={away} defense={home} usage={usage} eff={eff} asOfWeek={asOfWeek} />
    </section>
  );
}

/** Week 1: nothing to look up, since a row exists only after a team's first game. */
export function PlayersNotYet() {
  return (
    <section className="section players" aria-labelledby="players-h" data-players-none>
      <h2 id="players-h" className="section-label t-cap">
        Players · not used by the model
      </h2>
      <p className="t-small ink-2">No player rows before a team&apos;s first game of the season.</p>
    </section>
  );
}
