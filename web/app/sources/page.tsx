import type { Metadata } from "next";

import { SOURCES } from "@/lib/sources";

// /sources (docs/phases/P6.md §3, step 7): every source behind a displayed value, what it
// feeds, and its license. The list is lib/sources.ts; scripts/check-sources.mjs checks it
// covers every live signal. Build-time content only.

export const metadata: Metadata = { title: "Sources and licenses" };

export default function SourcesPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="t-title">Sources and licenses</h1>
        <p className="t-small ink-2">
          Every value on this site traces to one of these sources. Missing data stays missing; nothing is
          estimated to fill a gap.
        </p>
      </div>
      {SOURCES.map((s) => (
        <section key={s.id} className="section prose t-prose" id={s.id} aria-labelledby={`${s.id}-h`} data-source={s.id}>
          <h2 id={`${s.id}-h`} className="t-head">
            <a href={s.url}>{s.name}</a>
          </h2>
          <p>{s.feeds}</p>
          <p>
            License:{" "}
            {s.license.url ? <a href={s.license.url}>{s.license.name}</a> : s.license.name}
          </p>
          <ul>
            {s.terms.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        </section>
      ))}
      <section className="section prose t-prose">
        <h2 className="section-label t-cap">Not affiliated</h2>
        <p>
          This site is not affiliated with the NFL, any team, or any source above. It&apos;s non-commercial:
          no ads, subscriptions or paywall.
        </p>
      </section>
    </>
  );
}
