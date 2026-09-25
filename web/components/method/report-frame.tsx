import report from "@/content/backtest-report.json";
import { reportFrameView } from "@/lib/method";

// The backtest report, verbatim, inside a visible boundary (docs/phases/P6.md §3, step 7):
// a provenance heading naming the script, the date it was generated and the number of
// out-of-sample games, then an app-authored preface saying what the reader is about to
// see, then the report.
//
// This is the only module that reads content/backtest-report.json. tests/honesty.test.ts
// enforces that, and that the report never renders outside this frame.
//
// The HTML is marked's rendering of docs/backtest_report.md, a committed repo artifact,
// done at build time by scripts/sync-content.mjs. No runtime input reaches it.

export function ReportFrame() {
  const v = reportFrameView();
  return (
    <section className="section report-frame" id="report" aria-labelledby="report-h" data-report-frame>
      <h2 id="report-h" className="t-head" data-report-provenance>
        {v.heading}
      </h2>
      <div className="report-preface t-prose" data-report-preface>
        {v.preface.map((p) => (
          <p key={p}>{p}</p>
        ))}
      </div>
      <div className="report-body t-prose" data-report-body dangerouslySetInnerHTML={{ __html: report.html }} />
    </section>
  );
}
