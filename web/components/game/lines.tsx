import { FlipMarker, ValidationTag } from "@/components/marks";
import type { LinesView } from "@/lib/game";

// Lines block (docs/phases/P6.md §5, §6). Market rows, then the model, then the edge
// with its validation tag in the same cell, then input stability, then the typical miss.
// Columns are Spread and Total.

const DASH = <span className="null">—</span>;
const cell = (s: string | null) => (s === null ? DASH : s);

export function Lines({ v }: { v: LinesView }) {
  return (
    <section className="section" aria-labelledby="lines-h">
      <h2 id="lines-h" className="section-label t-cap">
        Lines
      </h2>
      <div className="table-scroll">
        <table className="data lines-table">
          <thead>
            <tr>
              <th scope="col" />
              <th scope="col" className="num">Spread</th>
              <th scope="col" className="num">Total</th>
            </tr>
          </thead>
          <tbody>
            {v.marketRows.map((r) => (
              <tr key={r.label} data-row={r.label === "Market at lock" ? "market-lock" : "market-latest"}>
                <th scope="row">
                  {r.label}
                  {r.note ? <span className="t-small ink-3"> · {r.note}</span> : null}
                </th>
                <td className="num">{cell(r.spread)}</td>
                <td className="num">{cell(r.total)}</td>
              </tr>
            ))}
            <tr className="model-row" data-row="model">
              <th scope="row">{v.model.label}</th>
              <td className="num">
                {v.model.flipped ? (
                  <>
                    <FlipMarker />{" "}
                  </>
                ) : null}
                {v.model.spread}
              </td>
              <td className="num">{v.model.total}</td>
            </tr>
            <tr className="edge-row" data-row="edge">
              <th scope="row">
                Model − market
                <span className="t-small ink-3"> · {v.locked ? "vs. the line at lock" : "vs. the latest line"}</span>
              </th>
              <td className="num">
                {v.edge.spread === null ? (
                  DASH
                ) : (
                  <>
                    {v.edge.spread} <ValidationTag tag={v.edge.tags.spread} />
                  </>
                )}
              </td>
              <td className="num">
                {v.edge.total === null ? (
                  DASH
                ) : (
                  <>
                    {v.edge.total} <ValidationTag tag={v.edge.tags.total} />
                  </>
                )}
              </td>
            </tr>
            <tr data-row="stability">
              <th scope="row">Input stability</th>
              <td
                colSpan={2}
                className="num"
                title="Share of each rating that comes from this season's plays and a trusted prior, rather than the league average. Not confidence in the result."
              >
                <span className="t-cap">{v.stability.bucket.toUpperCase()}</span> ({v.stability.min})
              </td>
            </tr>
            <tr data-row="typical-miss">
              <th scope="row">Typical miss, any projection (model or market)</th>
              <td className="num">≈{v.typicalMiss.margin} pts</td>
              <td className="num">≈{v.typicalMiss.total} pts</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  );
}
