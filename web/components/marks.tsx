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
