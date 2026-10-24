# p5-v2 backtest artifacts (superseded 2026-10-20)

`backtest_report.md` and `model_coefficients.json` exactly as published from commit
`0e1a720` (2026-09-30) until p5-v3, byte-identical copies.

They were computed on an offensive-line continuity input that was wrong for some teams in
2020 and 2021: 2020's combined O-line snap codes (`C/G`, `G/T`, …) weren't counted
(`docs/phases/P7.md`, open item 22, row 3).

/method builds its "earlier" column from these two files, with the same `buildSummary`
as the current figures (`web/scripts/sync-content.mjs`). Don't edit them. They're the
record of what the page said.
