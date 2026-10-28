import { SnapCountsCredit } from "@/components/marks";
import type { Card } from "@/lib/card";
import type { ClusterLabelT, SignalRowT } from "@/lib/db";
import { POSITION_VOCABULARY, availabilityVocabularyNote } from "@/lib/game";

// Availability counts (docs/phases/P6.md §6, context). The Availability analyst writes a
// cluster count only for teams with at least one flagged player (docs/signals.md), so a
// missing row means none flagged OR not computed for this week. It shows "—" and is
// never shown as 0.

const DASH = <span className="null">—</span>;
const MISSING = "No row: none flagged, or not computed this week";

/** `rows`: this week's signals for the game (Q6), and `weekLabels` the week's cluster-count
 *  labels (Q6b). Only labels are read, for the note on counts made before the
 *  position-code fix (docs/phases/P7.md, item 22). */
export function Availability({
  card,
  rows = [],
  weekLabels = [],
}: {
  card: Card;
  rows?: SignalRowT[];
  weekLabels?: ClusterLabelT[];
}) {
  const a = card.context.availability;
  const teams = [card.identity.away_team, card.identity.home_team];
  const note = availabilityVocabularyNote(rows, teams, weekLabels);
  const cell = (team: string, k: string) => {
    const v = a[team]?.[k] ?? null;
    return v === null ? <span title={MISSING}>{DASH}</span> : String(v);
  };
  return (
    <section className="section" aria-labelledby="avail-h">
      <h2 id="avail-h" className="section-label t-cap">
        Availability · not used by the model
      </h2>
      <table className="data avail-table">
        <thead>
          <tr>
            <th scope="col">Team</th>
            <th scope="col" className="num">O-line flagged</th>
            <th scope="col" className="num">Secondary flagged</th>
          </tr>
        </thead>
        <tbody>
          {teams.map((t) => (
            <tr key={t}>
              <th scope="row" className="mono">{t}</th>
              <td className="num">{cell(t, "ol_cluster_count")}</td>
              <td className="num">{cell(t, "secondary_cluster_count")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="legend t-small ink-2">
        Players listed with an injury designation or as unavailable (suspension, exempt list).
      </p>
      {note ? (
        <p className="legend t-small warn" data-vocabulary-note>
          {note.noRows
            ? `This week's O-line and secondary counts were computed before ${POSITION_VOCABULARY} and leave out ` +
              "offensive tackles and safeties coded SAF, so a blank here may be a team whose only flagged players " +
              "were those."
            : `These O-line and secondary counts were computed before ${POSITION_VOCABULARY} and leave out ` +
              "offensive tackles and safeties coded SAF."}
          {note.ramsMissing
            ? " There's no Rams row: a team-code mismatch kept Rams counts from being written (since fixed)."
            : ""}{" "}
          They aren&apos;t recomputed: that would put today&apos;s depth chart into a past week.
        </p>
      ) : null}
      <SnapCountsCredit use="availability" />
    </section>
  );
}
