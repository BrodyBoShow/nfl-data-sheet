import Link from "next/link";
import type { ReactNode } from "react";

import type { PlayerEff, PlayerUsage } from "@/lib/db";
import {
  FAMILIES,
  FTN_FIRST_SEASON,
  HIST_TABLES,
  BLEND_MARK,
  BLEND_TITLE,
  ROLE_GROUPS,
  TAGS,
  TACKLE_L4_MIN_DEF_SNAPS,
  cohortCaption,
  colLabel,
  countByGroup,
  defSnapShare,
  familyRows,
  formatSeasons,
  headlineL4,
  formatPlayerValue,
  histRows,
  l4SampleLabel,
  parseSpan,
  pctPopulation,
  posLabel,
  shownPct,
  splitFamilyRows,
  splitRoleRows,
  type Col,
  type Family,
  type Fmt,
  type HistTable,
  type Tag,
} from "@/lib/players";

// Players section (docs/phases/P7.md step 9). Each player's latest row through the week
// before the game, from Q7/Q8. Not used by the model. Nothing here is dimmed: stability
// is shown as a number beside the values (user decision 2026-09-30).
//
// Default visibility (docs/phases/P11.md, B): a table shows the players the analyst
// ranked; the rest sit under it, counted, in a collapsed copy of the same table. Defense
// is collapsed whole and isn't split (see lib/players.ts).

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
  col,
  blended = false,
}: {
  label: string;
  tags: Tag[];
  title?: string;
  sticky?: boolean;
  num?: boolean;
  colSpan?: number;
  rowSpan?: number;
  /** The data-col of the cells under this header, where one header heads one column. */
  col?: string;
  /** The column's values are blended estimates; the label carries BLEND_MARK. */
  blended?: boolean;
}) {
  const cls = [num ? "num" : "", sticky ? "sticky" : ""].filter(Boolean).join(" ") || undefined;
  return (
    <th
      scope="col"
      className={cls}
      title={title}
      colSpan={colSpan}
      rowSpan={rowSpan}
      data-tags={tags.join(",")}
      data-col={col}
      data-blended={blended ? "" : undefined}
    >
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
  row: { player_id: string; display_name: string | null; position: string | null; position_group: string | null; week: number };
  asOfWeek: number;
}) {
  const earlier = row.week < asOfWeek;
  const pos = posLabel(row);
  return (
    <>
      <th scope="row" className="sticky" data-player={row.player_id}>
        {row.display_name ?? <span className="mono">{row.player_id}</span>}
      </th>
      <td className="mono" data-pos>{pos ?? DASH}</td>
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
    <Head label="Pos" tags={[]} num={false} title="Position, and the position group a percentile ranks within when it differs." />
    <Head label="Last wk" tags={[]} title="The week of the player's latest game through the week before this one." />
  </>
);

/** Rows a table doesn't show by default, counted, with the same table inside. */
function Hidden({ kind, summary, children }: { kind: string; summary: string; children: ReactNode }) {
  return (
    <details className="players-hidden" data-hidden-rows={kind}>
      <summary className="t-small ink-2">{summary}</summary>
      {children}
    </details>
  );
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

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

function Role({ usage, eff, team, asOfWeek }: { usage: PlayerUsage[]; eff: PlayerEff[]; team: string; asOfWeek: number }) {
  const { shown, offensiveLine, hidden } = splitRoleRows(usage, eff, team);
  return (
    <>
      <RoleTable rows={shown} team={team} asOfWeek={asOfWeek} />
      {offensiveLine.length ? (
        <Hidden kind="offensive-line" summary={`${team} offensive line · ${plural(offensiveLine.length, "player", "players")} · snap %`}>
          <RoleTable rows={offensiveLine} team={team} asOfWeek={asOfWeek} />
        </Hidden>
      ) : null}
      {hidden.length ? (
        <Hidden
          kind="role"
          summary={
            `${plural(hidden.length, `${team} player`, `${team} players`)} not ranked in passing, rushing or ` +
            `receiving · ${countByGroup(hidden)}`
          }
        >
          <RoleTable rows={hidden} team={team} asOfWeek={asOfWeek} />
        </Hidden>
      ) : null}
    </>
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
  // A league percentile column only when some row shows one (P6.md §4). Defense has none
  // while Pro Football Reference's defensive rows stay gated (P7 step 7; open item 9 closed
  // without a source switch).
  const showPct = rows.some((r) => shownPct(family, r) !== null);
  const isDefense = family.id === "defense";
  const head = (c: Col<PlayerEff>) => (
    <Head
      key={c.key}
      label={colLabel(c)}
      tags={c.tags}
      title={c.title ? `${BLEND_TITLE} ${c.title}` : BLEND_TITLE}
      col={c.key}
      blended
    />
  );
  return (
    <div className="table-scroll">
      <table className="data players-table" data-family={family.id}>
        <caption className="t-small ink-2">
          {team} {family.title.toLowerCase()}
        </caption>
        <thead>
          <tr>
            {IDENTITY_HEADS}
            {isDefense ? (
              <Head
                label="Def snap %"
                tags={["PFR"]}
                title="Share of the team's defensive snaps, season. Observed, not blended."
                col="def_snap_share_std"
              />
            ) : null}
            <Head label={family.sample.label} tags={family.sample.tags} title="Season to date." col={family.sample.std} />
            <Head
              label={isDefense ? "Own % ‡" : "Own %"}
              tags={family.stability.tags}
              title={family.stability.title}
              col={family.stability.key}
            />
            <Head
              label={`${h.label} ${BLEND_MARK}`}
              tags={h.tags}
              title={`${BLEND_TITLE} Own % is the share of this value that is the player's own.`}
              col={h.std}
              blended
            />
            {showPct ? (
              <Head
                label={`Pct ${BLEND_MARK}`}
                tags={h.tags}
                title={`Percentile, 0–100, of the ${h.label} estimate, within the population under this table.`}
                col={h.pct}
                blended
              />
            ) : null}
            <Head
              label="Last 4"
              tags={h.tags}
              title="Last 4 games played, raw (not blended). Read with its sample."
              col={h.l4}
            />
            <Head label={l4SampleLabel(family)} tags={family.sample.tags} title="The last-4 sample." col={family.sample.l4} />
            {family.cols.map(head)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            // A percentile is shown only with its own population; a row that can't name
            // one (no position group) has its percentile withheld.
            const pct = shownPct(family, r);
            return (
              <tr key={r.player_id} data-row={r.player_id}>
                <Identity row={r} asOfWeek={asOfWeek} />
                {isDefense ? <Value col="def_snap_share_std" fmt="pct" v={defSnapShare(r, usage)} /> : null}
                <Value col={family.sample.std} fmt="count" v={r[family.sample.std] as number | null} />
                <td className="num" data-col={family.stability.key} data-stab>
                  {formatPlayerValue("stab", r[family.stability.key] as number | null) ?? DASH}
                </td>
                <Value col={h.std} fmt={h.fmt} v={r[h.std] as number | null} />
                {showPct ? (
                  <td
                    className="num"
                    data-col={h.pct}
                    data-pct-population={pct === null ? undefined : pctPopulation(family, r)!}
                    title={pct === null ? undefined : `Ranked among ${pctPopulation(family, r)}`}
                  >
                    {formatPlayerValue("rank", pct) ?? DASH}
                  </td>
                ) : null}
                <Value col={h.l4} fmt={h.fmt} v={headlineL4(family, r)} />
                <Value col={family.sample.l4} fmt="count" v={r[family.sample.l4] as number | null} />
                {family.cols.map((c) => (
                  <Value key={c.key} col={c.key} fmt={c.fmt} v={r[c.key] as number | null} />
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function FamilyNotes({ family, rows }: { family: Family; rows: PlayerEff[] }) {
  const isDefense = family.id === "defense";
  const approx = family.cols.filter((c) => c.approx);
  const showPct = rows.some((r) => shownPct(family, r) !== null);
  const cohort = cohortCaption(family, rows);
  return (
    <>
      {cohort ? (
        <p className="legend t-small ink-2" data-cohort>
          {cohort}
        </p>
      ) : null}
      {approx.length ? <p className="legend t-small ink-2">† {approx.map((c) => c.title).join(" ")}</p> : null}
      {isDefense && !showPct ? (
        <p className="legend t-small ink-2" data-not-ranked>
          Not ranked: Pro Football Reference has no defensive row for some games a player played, so these rates
          can&apos;t be compared fairly across players.
        </p>
      ) : null}
      {isDefense ? (
        <p className="legend t-small ink-2">
          ‡ Defense Own % rests on tackles, counted over games with a Pro Football Reference defensive row.
          Rates are per 100 defensive snaps. Tackle rates need a minimum of defensive snaps to mean anything, so
          Last 4 is blank under {TACKLE_L4_MIN_DEF_SNAPS}.
        </p>
      ) : null}
    </>
  );
}

/** An offense family: the ranked players, then the rest counted under it. */
function FamilySection({
  family,
  eff,
  team,
  asOfWeek,
  usage,
}: {
  family: Family;
  eff: PlayerEff[];
  team: string;
  asOfWeek: number;
  usage: PlayerUsage[];
}) {
  const { shown, belowMinimum, noGroup } = splitFamilyRows(eff, team, family);
  const rest = [...belowMinimum, ...noGroup];
  if (shown.length === 0 && rest.length === 0) return null;
  const reasons = [
    belowMinimum.length ? `${belowMinimum.length} below the per-game minimum` : null,
    noGroup.length ? `${noGroup.length} with no position group` : null,
  ].filter(Boolean);
  const title = family.title.toLowerCase();
  return (
    <div className="players-family" data-family-wrap={family.id}>
      <FamilyTable family={family} rows={shown} team={team} asOfWeek={asOfWeek} usage={usage} />
      {rest.length ? (
        <Hidden
          kind={family.id}
          summary={`${plural(rest.length, `${team} player`, `${team} players`)} in ${title} not ranked · ${reasons.join(", ")}`}
        >
          <FamilyTable family={family} rows={rest} team={team} asOfWeek={asOfWeek} usage={usage} />
        </Hidden>
      ) : null}
      <FamilyNotes family={family} rows={shown} />
    </div>
  );
}

/** Defense: collapsed whole, every row inside (not split; see lib/players.ts). */
function Defense({
  family,
  eff,
  team,
  asOfWeek,
  usage,
}: {
  family: Family;
  eff: PlayerEff[];
  team: string;
  asOfWeek: number;
  usage: PlayerUsage[];
}) {
  const rows = familyRows(eff, team, family);
  if (rows.length === 0) return null;
  return (
    <details className="players-family players-defense" data-family-wrap={family.id}>
      <summary className="t-cap ink-2">
        {team} defense · {plural(rows.length, "player", "players")} · not ranked
      </summary>
      <FamilyTable family={family} rows={rows} team={team} asOfWeek={asOfWeek} usage={usage} />
      <FamilyNotes family={family} rows={rows} />
    </details>
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
  // The seasons the shown rows cover between them. Each row's own span is in its row.
  const spans = formatSeasons(shown.flatMap((r) => parseSpan(r.hist_span) ?? []));
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
      <Role usage={usage} eff={eff} team={offense} asOfWeek={asOfWeek} />
      {[passing!, rushing!, receiving!].map((f) => (
        <FamilySection key={f.id} family={f} eff={eff} team={offense} asOfWeek={asOfWeek} usage={usage} />
      ))}
      <Defense family={def!} eff={eff} team={defense} asOfWeek={asOfWeek} usage={usage} />
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
        Each player&apos;s latest game through week {asOfWeek}. Nothing is dimmed. Last-4 values, game and play
        counts, and role shares are observed, not blended: read last-4 values with their sample.
      </p>
      <p className="legend t-small ink-2" data-visibility-legend>
        Shown by default: the players ranked in passing, rushing or receiving. Everyone else is counted under each
        table, with their rows one click away. Defense is collapsed.
      </p>
      <p className="legend t-small ink-2" data-blend-legend>
        {BLEND_MARK} Starred season values are estimates, not observed rates: each blends the player&apos;s own rate so far
        with the player&apos;s last season and the position group&apos;s current average, more so the fewer plays
        behind it, so a stat correction to anyone in the group can move it. The group is the player&apos;s position
        group (e.g. WR, DL), season to date, not the whole league. Own % is the share that is the player&apos;s own,
        this season and last, for the headline column only (EPA/dropback, EPA/carry, EPA/target, tackles per 100
        snaps); every other starred column blends at its own rate, which isn&apos;t shown. Pct ranks the headline
        estimate.
      </p>
      <p className="legend t-small ink-2" data-pct-legend>
        There is no single population behind Pct. Each percentile ranks a player within their own position group
        (Pos), among players meeting that group&apos;s per-game minimum (under each table), as of the row&apos;s Last
        wk. Rows from different weeks or position groups are ranked against different players.
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
