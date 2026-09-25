import Link from "next/link";

import { backtest, statusLine } from "@/lib/backtest";

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
      </span>
    </p>
  );
}
