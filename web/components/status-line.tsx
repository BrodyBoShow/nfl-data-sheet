import Link from "next/link";

// The backtest status line (docs/phases/P6.md §5, device 1). Step 4 fills it from
// docs/backtest_report.md at build time. Until then it shows no figures, rather than
// placeholder numbers that could be mistaken for the result.
export function StatusLine() {
  return (
    <p className="status-line shell t-small mono" aria-label="Model backtest status">
      Backtest vs. closing line: figures load from the backtest report (build step 4).{" "}
      <Link href="/method">method</Link>
    </p>
  );
}
