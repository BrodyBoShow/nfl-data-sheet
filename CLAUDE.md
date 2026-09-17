# NFL Data Sheet

Free-to-run NFL analytics system. Autonomous deterministic Python jobs (GitHub Actions
cron) collect data, analysts turn it into standardized signals, a synthesizer builds
per-game matchup cards, a Next.js app displays it. Full brief: `KICKOFF.md`. Architecture
graph and layer rules: `docs/architecture.md`. Phase specs: `docs/phases/P1.md`…`P8.md`.

## Hard constraints
- **$0/month.** No paid services except the small optional exceptions in Phase 8.
- **No LLMs in Phases 1–7.** "Agents" are deterministic Python jobs, not LLM calls.
- **Sourced data only.** Never fabricate, estimate, or fill gaps. Missing stays null.
- **Never guess a URL or field name.** Verify live, save a fixture, document it in
  `docs/sources.md` before writing a collector.

## Layer rules (non-negotiable)
- **L1 collectors** (`pipeline/collectors/`) fetch, validate, store. Never compute metrics.
- **L2 analysts** (`pipeline/analysts/`) read only stored tables, never external sources.
  Write only to `signals`.
- **L3** (`pipeline/synthesis/`, `/web`) reads `signals` only, never calls external sources.
- **L0** (`pipeline/orchestration/`) decides what runs, owns canonical keys, detects
  breakage, grades projections.

## Canonical keys
- `season` int, `week` int, `season_type` text (`REG`/`POST`)
- `game_id` text, nflverse format: `2026_02_KC_BUF`
- `team` text, nflverse abbreviations
- `player_id` text = gsis ID; other providers' IDs live only in the ID crosswalk
- All timestamps stored **UTC** (`timestamptz`); display ET in the UI

## Data contracts
- `signals` table shape and the signal registry: `docs/signals.md`
- `agent_runs` table: `id, agent, started_at, finished_at, status, rows_written,
  source_version, error, meta jsonb`
- `Collector`: `should_run(ctx) -> bool` → `fetch(ctx)` → `validate(raw)` →
  `store(ctx, validated) -> int`
- `Analyst`: `inputs_ready(ctx) -> bool` → `compute(ctx) -> polars.DataFrame` →
  `write_signals(ctx, df) -> int`
- `ctx` (`RunContext`) carries `season, week, season_type, now, settings`, and one open
  `conn` shared by every step of that run (so `store`/`write_signals` write through it,
  and `should_run`/`inputs_ready` can query state like `source_freshness` through it too).
- `run()` wraps everything, logs to `agent_runs`, never raises past the run boundary —
  one job's exception is caught, logged as `status="failed"`, and swallowed. Both
  hash-diff before upserting (`pipeline/core/db.py`'s `filter_changed`, keyed on a
  `content_hash` column each table carries).
- `source_freshness` table (`source text PK, last_value text, checked_at`): generic
  freshness-gate state for any collector checking a cheap upstream marker (e.g.
  nflverse's `timestamp.json`) before a full fetch — not in the original P1 file list,
  added once the ID spine collector needed it.

## Stack
Python 3.12, uv, Polars, **nflreadpy** (not nfl_data_py — deprecated), httpx, pydantic,
psycopg 3, pytest, ruff, mypy. Supabase Postgres (free tier), plain SQL migrations in
`db/migrations/NNNN_name.sql`. Next.js App Router + TypeScript in `/web` (Phase 6+, don't
scaffold early). GitHub Actions for scheduling, UTC crons, never on `:00`.
- Never edit a migration file once it's been applied — not even comments. `db/migrate.py`
  tracks applied migrations by filename only (`schema_migrations.id = path.stem`, no
  checksum), so it won't detect or warn about drift between the file on disk and what
  actually ran. Any schema change or correction, however small, goes in a new
  `NNNN_name.sql` migration.

## Repo layout
```
docs/architecture.md, sources.md, signals.md, phases/P1.md..P8.md
db/migrations/, db/migrate.py   # migration runner: uv run python db/migrate.py
pipeline/core/            # base classes, db, logging, hashing, freshness, config
pipeline/collectors/
pipeline/analysts/
pipeline/orchestration/   # dispatcher, auditor, grader
pipeline/synthesis/
scripts/                  # one-off scripts (fixture generation, backfills), not scheduled jobs
tests/fixtures/           # one trimmed real response per source
tests/
.github/workflows/
web/                      # Phase 6+
```

## Conventions
- Signal names: `snake_case`, e.g. `epa_per_dropback_adj`.
- Never raw play-by-play in Postgres — aggregates only.
- Tests use fixtures, never live calls. Fixtures are trimmed samples, not full responses.
- Freshness gate: check each source's own change marker (e.g. nflverse `timestamp.json`)
  before refetching; skip unchanged.
- Weeks 1–5 efficiency is noisy — prior-blend with last season, shifting weight to
  current season as weeks accumulate (see `docs/signals.md`).

## Agent scoping (one file, one job)
- One collector per source family, one analyst per sector — never combine two sources or
  two sectors in one file, and never let one reach into another's tables directly (they
  only share state through `signals`/staged tables, per the layer rules above).
- Every collector/analyst module opens with a short docstring stating its single job:
  ```python
  """
  Job: <one line — what this agent does, nothing else>
  Reads: <source(s) or staged table(s)>
  Writes: <table>
  Tier: T0 | T1 | T2 | T3 | OD
  Phase: <P#, from docs/phases/>
  """
  ```
- If an agent's job needs "and" to describe (e.g. "fetches odds and computes movement"),
  it's two agents — split it before writing code.
- Cross-check new agents against `docs/architecture.md`'s piece-by-piece tables before
  creating a file — the job, tier, and phase should already be listed there; update the
  table first if it isn't, don't let code and docs diverge.

## Working rules
- Plan before code; wait for go-ahead on each phase.
- One phase per session. Update the phase file's status and any changed `docs/` at the
  end of each phase.
- Don't run long pipelines yourself — give the user the command to run.
- Never load full datasets into context; inspect with schema/`head(5)`/row counts only.
- Supabase MCP is read-only here — schema changes go through `db/migrations/` only.
- Flag anything in `KICKOFF.md` you think is wrong, with the reason, instead of silently
  deviating.

## Git / commit workflow
- Before every commit, run `git status` and confirm `.env` and anything under `data/` or
  `.cache/` is not staged. If it is, stop and tell the user instead of committing.
- Commit at the end of each task the user approves, with a clear message.
- Never run `git push`, `git reset --hard`, `git rebase`, or anything else that rewrites
  history. Pushing waits until the user explicitly says so.
