import Link from "next/link";

import { backtest, previousBacktest, statusLine } from "@/lib/backtest";
import { correctionTooltip } from "@/lib/method";

// The backtest status line (docs/phases/P6.md §5, device 1): one line in the header
// rule on desktop (scripts/check-layout.mjs checks it at 1280px). Every figure comes
// from docs/backtest_report.md and model_coefficients.json at build time
// (scripts/sync-content.mjs). The validation phrase links to the evidence. On phones
// the line wraps only at its separators.
export function StatusLine() {
  const s = statusLine(backtest);
  return (
    <p className="status-line shell t-small mono" aria-label="Model backtest status">
      <span className="nowrap">{s.scope}:</span>{" "}
      <span className="nowrap">
        margin MAE model {s.marginModel} · closing line {s.marginClose}
      </span>{" "}
      <span className="nowrap">
        · <span title={s.edgeExact}>edge vs result r {s.edgeR} {s.edgeCi}</span>
      </span>{" "}
      <span className="nowrap">
        · <Link href="/method#edge">{s.validation}</Link>
        {/* docs/phases/P7.md open item 22, row 3: the marker travels with the numbers (as in
            item 19), and links to the /method correction. An erratum is the published
            correction, so the word stays once the figures are fixed, and its width with it.
            honesty.test.ts 16. No separator or space before it, only .erratum's margin. §5
            keeps the line on one line at 1280px: it had 65px to spare before the tag and has
            1.9px after (measured 2026-10-18, 1086.1 of 1088px). A longer figure wraps it;
            rerun scripts/check-layout.mjs. */}
        <Link
          href="/method#correction"
          className="tag erratum"
          title={correctionTooltip(previousBacktest, backtest)}
        >
          ERRATUM
        </Link>
      </span>
    </p>
  );
}
