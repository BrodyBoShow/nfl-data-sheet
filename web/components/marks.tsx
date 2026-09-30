import Link from "next/link";

import type { ValidationTag as Tag } from "@/lib/game";

// Marks shared by the week and game views (docs/phases/P6.md §5).

/** The model favors the other team from the market (option A, user decision
 *  2026-09-24). It's a word, so it carries meaning without color. It carries no number
 *  and looks the same for a 0.2-point flip and a 10-point one (§8 Q1). Callers put a
 *  real space after it. */
export function FlipMarker() {
  return (
    <span
      className="flip t-small ink-2"
      title="The model and the market favor different teams. How far apart they are isn't marked."
    >
      flipped
    </span>
  );
}

/** The Sports Reference credit, under each block whose values rest on Pro Football
 *  Reference snap counts (docs/sources.md, nflverse bulk → License: "next to every
 *  PFR-derived value"). Stated once per block, not per value. Efficiency: the O-line
 *  continuity adjustment, which reaches defense ratings through the opponent adjustment.
 *  Availability: a snap clears a player ESPN still lists (availability_impact.py,
 *  _fetch_last_played). */
export function SnapCountsCredit({ use }: { use: "efficiency" | "availability" }) {
  const what =
    use === "efficiency"
      ? "Efficiency ratings include an O-line continuity adjustment built from snap counts"
      : "A player ESPN still lists but who has played since is cleared using snap counts";
  return (
    <p className="legend t-small ink-2" data-credit="pfr">
      {what} by Pro Football Reference (Sports Reference LLC), via nflverse (
      <Link href="/sources#pfr">sources</Link>).
    </p>
  );
}

/** The validation status that travels with every edge value (§5 device 2). It's part of
 *  the value, not a footnote. It links to the evidence, and its tooltip gives this
 *  card's bucket figures. */
export function ValidationTag({ tag }: { tag: Tag }) {
  const label = tag.validated ? "VALIDATED" : "NOT VALIDATED";
  const title =
    tag.evidence ??
    (tag.validated
      ? "Validated against the closing line in the backtest."
      : "Not shown to beat the closing line in the backtest.");
  return (
    <Link href="/method#edge" className="tag" title={title}>
      {label}
    </Link>
  );
}
